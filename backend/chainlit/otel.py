"""OpenTelemetry export from the Step tree.

Slice 1 of the KRE-338 design (§11). Chainlit owns its tracer provider, exporter
and processor (D1): app spans never pass through Chainlit's path, and Chainlit
never mutates or shuts down a provider it does not own.

Every public hook here is total. Telemetry must never change the outcome of a
turn, so failures are logged once and the hook returns a no-op handle.

The SDK and exporter are optional (``chainlit[otel]``). This module imports
them lazily so ``import chainlit`` stays free of them when telemetry is off.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from chainlit.config import config
from chainlit.logger import logger
from chainlit.session import is_valid_thread_id
from chainlit.version import __version__

#: Hard cap on step spans per turn (§9). The root span is not counted, so a
#: turn always exports at most ``MAX_SPANS_PER_TURN + 1`` spans.
MAX_SPANS_PER_TURN = 200

_DEFAULT_ENDPOINT = "http://localhost:4318/v1/traces"
_DEFAULT_SERVICE_NAME = "chainlit"

# RFC 7230 token, the only charset OTLP header names may use.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
# RFC 7230 field-value minus obs-text: printable ASCII and space only. CR, LF,
# NUL, other C0/DEL and non-ASCII values are dropped before an exporter can
# reject and log them (D3, §6.3).
_HEADER_VALUE_RE = re.compile(r"[\x20-\x7e]*")

# §5.4: the only attribute keys any span may carry.
_ATTRIBUTE_KEYS = frozenset(
    {
        "chainlit.span.unclosed",
        "chainlit.step.is_error",
        "chainlit.step.name",
        "chainlit.step.type",
        "chainlit.telemetry.dropped_spans",
        "chainlit.thread.id",
        "chainlit.turn.cancelled",
        "chainlit.turn.kind",
        "error.type",
    }
)

# §4.4: a dynamic exception class name must not bypass the bound.
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")

_STEP_TYPES = {"run", "tool", "llm", "embedding", "retrieval", "rerank", "undefined"}
_CLIENT_STEP_TYPES = {"tool", "llm", "embedding", "retrieval", "rerank"}

_QUEUE_FULL_RE = re.compile(r"queue (is )?full")

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

_init_lock = threading.Lock()
_headers_lock = threading.Lock()
_initialized = False
_failed = False
_provider: Any = None
_tracer: Any = None
_warned: set[str] = set()

_turn: ContextVar[Optional[_Turn]] = ContextVar("chainlit_otel_turn", default=None)


@dataclass
class _StepHandle:
    span: Any
    activation: Any
    ended: bool = False


@dataclass
class _Turn:
    root: Any
    trace_id: int
    activation: Any = None
    spans: Dict[str, _StepHandle] = field(default_factory=dict)
    count: int = 0
    dropped: int = 0
    error_type: Any = None
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


# ---------------------------------------------------------------------------
# Init / shutdown
# ---------------------------------------------------------------------------


def init() -> None:
    """Create the provider, exporter and processor. Total: never raises."""
    global _initialized, _provider, _tracer

    with _init_lock:
        if _initialized or _failed:
            return
        _initialized = True

    settings = getattr(config.features, "telemetry", None)
    if settings is None or not settings.enabled:
        return

    if os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return
    if os.environ.get("OTEL_TRACES_EXPORTER", "").strip().lower() == "none":
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _disable(
            "telemetry: [features.telemetry] is enabled but the 'otel' extra is "
            "not installed; run `pip install chainlit[otel]`. Telemetry is disabled."
        )
        return
    except Exception:
        _disable("telemetry: provider initialization failed. Telemetry is disabled.")
        return

    try:
        exporter, endpoint, header_names, dropped_headers = _build_exporter()
        _install_endpoint_redaction_filter(endpoint)

        provider = TracerProvider(
            resource=_resource(Resource),
            sampler=_sampler(settings.sample_ratio),
        )
        provider.add_span_processor(_processor(BatchSpanProcessor, exporter))
        _install_queue_full_filter()
    except Exception:
        _disable("telemetry: provider initialization failed. Telemetry is disabled.")
        return

    logger.info(f"telemetry: exporting traces to {_loggable_endpoint(endpoint)}")
    if dropped_headers or header_names:
        logger.warning(
            "telemetry: dropped %d malformed OTLP header %s; using headers: %s",
            dropped_headers,
            "entry" if dropped_headers == 1 else "entries",
            ", ".join(header_names) or "none",
        )
    _warn_if_insecure(endpoint)

    try:
        if isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            trace.set_tracer_provider(provider)
        else:
            logger.info(
                "telemetry: using Chainlit's own tracer provider; "
                "the app provider was not modified"
            )
    except Exception:
        _disable(
            "telemetry: could not install the tracer provider. Telemetry is disabled."
        )
        return

    _provider = provider
    _tracer = provider.get_tracer(__name__, __version__)


def shutdown() -> None:
    """Flush and stop Chainlit's provider without blocking process exit (D4)."""
    global _provider, _tracer

    with _init_lock:
        provider = _provider
        _provider = None
        _tracer = None

    if provider is None:
        return

    def run() -> None:
        try:
            provider.shutdown()
        except BaseException:
            pass

    # The SDK's shutdown() has no enforceable timeout (§7), so the flush runs
    # on a daemon thread and the caller only waits for the join budget.
    thread = threading.Thread(target=run, name="chainlit-otel-shutdown", daemon=True)
    try:
        thread.start()
        thread.join(timeout=2.0)
    except Exception:
        _warn_once("shutdown", "telemetry: failed to shut down the tracer provider")


def _disable(message: str) -> None:
    global _failed, _provider, _tracer
    _failed = True
    _provider = None
    _tracer = None
    _warn_once("disabled", message)


def _resource(resource_cls: Any) -> Any:
    service_name = os.environ.get("OTEL_SERVICE_NAME") or _DEFAULT_SERVICE_NAME
    attributes: Dict[str, Any] = {
        "service.name": service_name,
        "service.version": __version__,
    }
    # The SDK's resource detector logs malformed pairs raw, so parse the env
    # var ourselves and keep it out of the detector (D3, §6.3).
    raw = os.environ.pop("OTEL_RESOURCE_ATTRIBUTES", None)
    try:
        if raw:
            attributes.update(_parse_resource_attributes(raw))
        return resource_cls.create(attributes)
    finally:
        if raw is not None:
            os.environ["OTEL_RESOURCE_ATTRIBUTES"] = raw


def _parse_resource_attributes(raw: str) -> Dict[str, str]:
    """Parse OTEL_RESOURCE_ATTRIBUTES without ever logging an entry (D3)."""
    attributes: Dict[str, str] = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        key = key.strip()
        if separator and key:
            attributes[key] = unquote(value.strip())
    return attributes


def _sampler(sample_ratio: float) -> Any:
    # An explicit sampler would override the standard OTel env, so only choose
    # the Chainlit default when the operator has not asked for something else.
    if os.environ.get("OTEL_TRACES_SAMPLER", "").strip():
        return None

    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    return ParentBased(TraceIdRatioBased(sample_ratio))


def _build_exporter() -> Tuple[Any, str, List[str], int]:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    if protocol and protocol != "http/protobuf":
        logger.warning(
            "telemetry: OTEL_EXPORTER_OTLP_PROTOCOL=%s is not supported; using OTLP/HTTP",
            protocol,
        )

    # The SDK parses and logs header env vars in the exporter constructor, so
    # both are removed while Chainlit parses and passes them explicitly (D3).
    with _headers_lock:
        traces_headers = os.environ.pop("OTEL_EXPORTER_OTLP_TRACES_HEADERS", None)
        general_headers = os.environ.pop("OTEL_EXPORTER_OTLP_HEADERS", None)
        try:
            raw = traces_headers or (general_headers or "")
            headers, names, dropped = _parse_otlp_headers(raw)
            endpoint = _resolve_endpoint()
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers, timeout=5.0)
        finally:
            if traces_headers is not None:
                os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = traces_headers
            if general_headers is not None:
                os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = general_headers

    return exporter, endpoint, names, dropped


def _resolve_endpoint() -> str:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint:
        root = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        endpoint = root.rstrip("/") + "/v1/traces" if root else _DEFAULT_ENDPOINT
    _validate_endpoint(endpoint)
    return endpoint


def _validate_endpoint(endpoint: str) -> None:
    """Reject endpoints an exporter error could echo verbatim (D3)."""
    if not endpoint or not _HEADER_VALUE_RE.fullmatch(endpoint):
        raise ValueError("invalid OTLP endpoint")
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("invalid OTLP endpoint")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("invalid OTLP endpoint")
    if parsed.query or parsed.fragment or parsed.params:
        raise ValueError("invalid OTLP endpoint")
    parsed.port  # raises ValueError for a malformed or out-of-range port


def _loggable_endpoint(endpoint: str) -> str:
    try:
        parsed = urlparse(endpoint)
        port = f":{parsed.port}" if parsed.port else ""
        if parsed.scheme and parsed.hostname:
            return f"{parsed.scheme}://{parsed.hostname}{port}"
    except Exception:
        pass
    return "the configured OTLP endpoint"


def _warn_if_insecure(endpoint: str) -> None:
    try:
        parsed = urlparse(endpoint)
    except Exception:
        return
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        logger.warning(
            "telemetry: the OTLP endpoint uses plaintext http to a non-loopback "
            "host; put credentials in headers, not in the URL"
        )


def _parse_otlp_headers(raw: str) -> Tuple[Dict[str, str], List[str], int]:
    """Parse OTLP header env vars without ever logging an entry (D3)."""
    headers: Dict[str, str] = {}
    dropped = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, separator, value = entry.partition("=")
        name = name.strip(" ")
        if not separator or not _HEADER_NAME_RE.fullmatch(name):
            dropped += 1
            continue
        value = unquote(value.strip())
        if not _HEADER_VALUE_RE.fullmatch(value):
            dropped += 1
            continue
        headers[name] = value
    return headers, sorted(headers), dropped


def _attributes(values: Dict[str, Any]) -> Dict[str, Any]:
    """Return only allowlisted attributes (§5.4)."""
    return {key: value for key, value in values.items() if key in _ATTRIBUTE_KEYS}


def _set_attributes(span: Any, values: Dict[str, Any]) -> None:
    for key, value in _attributes(values).items():
        span.set_attribute(key, value)


def _processor(processor_cls: Any, exporter: Any) -> Any:
    """Export only Chainlit's own spans (D1).

    Telemetry may install its provider as the global one (AC 8); other
    instrumentations then create spans on it too. Their spans must not leave
    the process through Chainlit's exporter.
    """

    class _ChainlitSpanProcessor(processor_cls):  # type: ignore[misc, valid-type]
        def on_end(self, span: Any) -> None:
            scope = getattr(span, "instrumentation_scope", None)
            if scope is not None and scope.name == __name__:
                super().on_end(span)

    return _ChainlitSpanProcessor(
        exporter,
        max_queue_size=_env_int("OTEL_BSP_MAX_QUEUE_SIZE", 2048),
        schedule_delay_millis=_env_int("OTEL_BSP_SCHEDULE_DELAY", 5000),
        max_export_batch_size=_env_int("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", 512),
    )


_ENDPOINT_LOGGERS = (
    "opentelemetry.exporter.otlp.common.http",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
)


class _EndpointRedactionFilter(logging.Filter):
    """Strip the endpoint path from exporter logs (D3, AC 10)."""

    def __init__(self, endpoint: str) -> None:
        super().__init__()
        try:
            parsed = urlparse(endpoint)
            self._needles = tuple(
                needle for needle in {parsed.path, parsed.query} if len(needle) > 1
            )
        except Exception:
            self._needles = ()

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._needles:
            return True
        try:
            text = record.getMessage()
            for needle in self._needles:
                text = text.replace(needle, "[redacted]")
            if text != record.getMessage():
                record.msg = text
                record.args = ()
        except Exception:
            pass
        return True


def _install_endpoint_redaction_filter(endpoint: str) -> None:
    _remove_endpoint_redaction_filters()
    filter_ = _EndpointRedactionFilter(endpoint)
    for name in _ENDPOINT_LOGGERS:
        logging.getLogger(name).addFilter(filter_)


def _remove_endpoint_redaction_filters() -> None:
    for name in _ENDPOINT_LOGGERS:
        target = logging.getLogger(name)
        for filter_ in list(target.filters):
            if isinstance(filter_, _EndpointRedactionFilter):
                target.removeFilter(filter_)


class _QueueFullFilter(logging.Filter):
    """At most one "Queue full" warning per window, per §9 (S9)."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        super().__init__()
        self.window_seconds = window_seconds
        self._last = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage().lower()
        except Exception:
            return True
        if not _QUEUE_FULL_RE.search(message):
            return True
        now = time.monotonic()
        if now - self._last < self.window_seconds:
            return False
        self._last = now
        return True


def _install_queue_full_filter() -> None:
    # The processor lives in one of these modules depending on the SDK version.
    for name in (
        "opentelemetry.sdk._shared_internal",
        "opentelemetry.sdk.trace.export",
    ):
        sdk_logger = logging.getLogger(name)
        if not any(isinstance(f, _QueueFullFilter) for f in sdk_logger.filters):
            sdk_logger.addFilter(_QueueFullFilter())


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# App context
# ---------------------------------------------------------------------------


def get_tracer(name: str = "app") -> Any:
    """Return Chainlit's tracer, or a no-op tracer when telemetry is off."""
    if _tracer is not None:
        return _tracer
    try:
        from opentelemetry.trace import NoOpTracerProvider
    except ImportError:
        return _NullTracer()
    return NoOpTracerProvider().get_tracer(name)


def current_traceparent() -> Optional[str]:
    """Return the W3C traceparent of the current span, or None."""
    if _tracer is None:
        return None
    try:
        from opentelemetry import trace

        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return None
        return (
            f"00-{span_context.trace_id:032x}-{span_context.span_id:016x}-"
            f"{int(span_context.trace_flags):02x}"
        )
    except Exception:
        return None


class _NullSpan:
    def get_span_context(self) -> None:
        return None

    def is_recording(self) -> bool:
        return False

    def set_attribute(self, *args: Any, **kwargs: Any) -> None:
        pass

    def set_status(self, *args: Any, **kwargs: Any) -> None:
        pass

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def record_exception(self, *args: Any, **kwargs: Any) -> None:
        pass

    def update_name(self, *args: Any, **kwargs: Any) -> None:
        pass

    def end(self, *args: Any, **kwargs: Any) -> None:
        pass


class _NullTracer:
    """Fallback when telemetry is off and opentelemetry-api is absent."""

    def start_span(self, *args: Any, **kwargs: Any) -> _NullSpan:
        return _NullSpan()

    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Any:
        from contextlib import nullcontext

        return nullcontext(_NullSpan())


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


def start_turn(kind: str, thread_id: Optional[str] = None) -> Optional[_Turn]:
    """Start the root span of one turn (D2: no ambient parent)."""
    tracer = _tracer
    if tracer is None:
        return None
    try:
        from opentelemetry import context as otel_context, trace

        attributes: Dict[str, Any] = {"chainlit.turn.kind": kind}
        if is_valid_thread_id(thread_id):
            attributes["chainlit.thread.id"] = thread_id

        root = tracer.start_span(
            "chainlit.turn",
            context=otel_context.Context(),
            kind=trace.SpanKind.INTERNAL,
            attributes=_attributes(attributes),
            record_exception=False,
            set_status_on_exception=False,
        )
        activation = trace.use_span(
            root,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        )
        activation.__enter__()
        turn = _Turn(
            root=root,
            trace_id=root.get_span_context().trace_id,
            activation=activation,
        )
        _turn.set(turn)
        return turn
    except Exception:
        _warn_once("start_turn", "telemetry: failed to start a turn span")
        return None


def mark_turn_error(turn: Optional[_Turn], exc_type: Any) -> None:
    if turn is not None:
        turn.error_type = exc_type


def mark_turn_cancelled(turn: Optional[_Turn]) -> None:
    if turn is not None:
        turn.cancelled = True


def end_turn(turn: Optional[_Turn]) -> None:
    """Close open step spans, then the root. Total: never raises."""
    if turn is None:
        return
    try:
        _turn.set(None)
    except Exception:
        pass

    try:
        with turn.lock:
            handles = list(turn.spans.values())
            dropped = turn.dropped
        for handle in handles:
            if not handle.ended:
                handle.ended = True
                try:
                    _set_attributes(handle.span, {"chainlit.span.unclosed": True})
                    handle.span.end()
                except Exception:
                    pass
            _deactivate(handle)
        if dropped:
            _set_attributes(turn.root, {"chainlit.telemetry.dropped_spans": dropped})
        if turn.cancelled:
            _set_attributes(turn.root, {"chainlit.turn.cancelled": True})
        if turn.error_type is not None:
            _set_attributes(
                turn.root, {"error.type": _sanitize_error_type(turn.error_type)}
            )
            _set_error_status(turn.root)
        turn.root.end()
    except Exception:
        _warn_once("end_turn", "telemetry: failed to end the turn span")
    finally:
        activation = turn.activation
        if activation is not None:
            turn.activation = None
            try:
                activation.__exit__(None, None, None)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def start_step(step: Any) -> None:
    """Start the span for an entered Step. Total: never raises."""
    turn = _turn.get()
    tracer = _tracer
    if turn is None or tracer is None:
        return

    try:
        from opentelemetry import context as otel_context, trace

        step_type = _step_type(step)
        with turn.lock:
            if turn.count >= MAX_SPANS_PER_TURN:
                turn.dropped += 1
                return
            turn.count += 1
            parent = _parent_span(turn, step)
            name = _capture_step_name(step) if _capture_names() else ""
            attributes: Dict[str, Any] = {
                "chainlit.step.type": step_type,
                "chainlit.step.is_error": False,
            }
            if name:
                attributes["chainlit.step.name"] = name
            span = tracer.start_span(
                name or f"chainlit.{step_type}",
                context=trace.set_span_in_context(parent, otel_context.Context()),
                kind=(
                    trace.SpanKind.CLIENT
                    if step_type in _CLIENT_STEP_TYPES
                    else trace.SpanKind.INTERNAL
                ),
                attributes=_attributes(attributes),
                record_exception=False,
                set_status_on_exception=False,
            )
            handle = _StepHandle(span=span, activation=None)
            turn.spans[step.id] = handle

        handle.activation = trace.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        )
        handle.activation.__enter__()
        step._otel_span = handle
    except Exception:
        _warn_once("start_step", "telemetry: failed to start a step span")


def end_step(step: Any, exc_type: Any = None) -> None:
    """End the span for a Step leaving its context manager. Total: never raises."""
    handle = getattr(step, "_otel_span", None)
    if handle is None:
        return

    try:
        step._otel_span = None
        if handle.ended:
            return
        handle.ended = True

        span = handle.span
        if exc_type is not None or bool(getattr(step, "is_error", False)):
            _set_attributes(span, {"chainlit.step.is_error": True})
            _set_attributes(
                span,
                {
                    "error.type": _sanitize_error_type(
                        exc_type if exc_type is not None else "error"
                    )
                },
            )
            _set_error_status(span)
        span.end()
    except Exception:
        _warn_once("end_step", "telemetry: failed to end a step span")
    finally:
        _deactivate(handle)


def _parent_span(turn: _Turn, step: Any) -> Any:
    """Resolve the parent span per §4.2. Only same-trace spans are accepted."""
    from opentelemetry import trace

    parent_id = getattr(step, "parent_id", None)
    if parent_id:
        handle = turn.spans.get(parent_id)
        if handle is not None:
            return handle.span

    current = trace.get_current_span()
    if current is not None:
        span_context = current.get_span_context()
        if getattr(span_context, "is_valid", False) and (
            span_context.trace_id == turn.trace_id
        ):
            return current

    return turn.root


def _step_type(step: Any) -> str:
    value = getattr(step, "type", "undefined")
    return value if value in _STEP_TYPES else "undefined"


def _capture_names() -> bool:
    settings = getattr(config.features, "telemetry", None)
    return bool(settings is not None and settings.capture_names)


def _capture_step_name(step: Any) -> str:
    """Bound and sanitize an opt-in step name (§4.4)."""
    name = getattr(step, "name", "")
    if not isinstance(name, str):
        return ""
    cleaned = "".join(
        char for char in name if unicodedata.category(char) not in ("Cc", "Cf")
    )
    return cleaned[:128]


def _sanitize_error_type(exc_type: Any) -> str:
    name = getattr(exc_type, "__name__", None)
    if isinstance(name, str) and _ERROR_TYPE_RE.match(name):
        return name
    return "error"


def _set_error_status(span: Any) -> None:
    from opentelemetry.trace import Status, StatusCode

    span.set_status(Status(StatusCode.ERROR, ""))


def _deactivate(handle: _StepHandle) -> None:
    activation = handle.activation
    if activation is None:
        return
    handle.activation = None
    try:
        activation.__exit__(None, None, None)
    except Exception:
        pass


def _warn_once(key: str, message: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message)


def _reset() -> None:
    """Reset module state. Test-only."""
    global _initialized, _failed, _provider, _tracer
    with _init_lock:
        _initialized = False
        _failed = False
        _provider = None
        _tracer = None
        _warned.clear()
    for name in (
        "opentelemetry.sdk._shared_internal",
        "opentelemetry.sdk.trace.export",
    ):
        sdk_logger = logging.getLogger(name)
        for filter_ in list(sdk_logger.filters):
            if isinstance(filter_, _QueueFullFilter):
                sdk_logger.removeFilter(filter_)
    _remove_endpoint_redaction_filters()
    _turn.set(None)
