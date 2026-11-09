"""Slice 1 of the KRE-338 OpenTelemetry design: disabled-safe core export."""

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import chainlit.otel as otel
from chainlit.config import TelemetryFeature, config
from chainlit.context import ChainlitContext, context_var
from chainlit.element import Text
from chainlit.emitter import ChainlitEmitter
from chainlit.server import app as chainlit_app, lifespan
from chainlit.step import Step, step as step_decorator

THREAD_ID = "3b0f7f7e-2f1f-4a7b-9c5e-9f1f2a3b4c5d"
SENTINEL = "SENTINEL_KRE338_SECRET"
BACKEND_DIR = Path(__file__).resolve().parents[1]

OTEL_ENV = (
    "OTEL_SDK_DISABLED",
    "OTEL_TRACES_EXPORTER",
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_SERVICE_NAME",
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_BSP_MAX_QUEUE_SIZE",
    "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
    "OTEL_BSP_SCHEDULE_DELAY",
)


@pytest.fixture(autouse=True)
def otel_isolation(monkeypatch):
    """Clean env, SDK global-provider slot and module state for each test."""
    import opentelemetry.trace as trace_api

    for name in OTEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None, raising=False)
    monkeypatch.setattr(
        trace_api._TRACER_PROVIDER_SET_ONCE, "_done", False, raising=False
    )
    # Telemetry does not depend on a data layer; keep the tests hermetic so the
    # optional `otel` extra is the only requirement.
    for name in (
        "chainlit.data",
        "chainlit.step",
        "chainlit.element",
        "chainlit.message",
        "chainlit.emitter",
        "chainlit.socket",
        "chainlit.server",
    ):
        module = importlib.import_module(name)
        if hasattr(module, "get_data_layer"):
            monkeypatch.setattr(module, "get_data_layer", lambda: None)
    otel._reset()
    yield
    otel.shutdown()
    otel._reset()


def _enable_config(monkeypatch, sample_ratio: float = 1.0) -> None:
    monkeypatch.setattr(
        config.features,
        "telemetry",
        TelemetryFeature(enabled=True, sample_ratio=sample_ratio),
    )


def _init_with_exporter(monkeypatch, exporter, sample_ratio: float = 1.0):
    """Initialize telemetry with an in-memory exporter instead of OTLP."""
    _enable_config(monkeypatch, sample_ratio=sample_ratio)
    monkeypatch.setattr(
        otel,
        "_build_exporter",
        lambda: (exporter, "http://localhost:4318/v1/traces", [], 0),
    )
    otel.init()
    assert otel._provider is not None
    return exporter


def _finished(exporter):
    otel._provider.force_flush()
    return exporter.get_finished_spans()


@pytest.fixture
def memory_exporter():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    return InMemorySpanExporter()


@pytest.fixture
async def ws(monkeypatch, mock_chainlit_context):
    """A real ChainlitEmitter on the mock session, wired into chainlit.socket."""
    import chainlit.socket as socket

    async with mock_chainlit_context as base:
        session = base.session
        session.id = "otel-session"
        session.thread_id = THREAD_ID
        session.files = {}
        session.files_spec = {}
        session.emit = AsyncMock()
        session.emit_call = AsyncMock()
        session.unavailable_chat_profile = None
        session.has_first_interaction = True
        session.current_task = None

        emitter = ChainlitEmitter(session)
        ctx = ChainlitContext(session, emitter=emitter)
        token = context_var.set(ctx)

        monkeypatch.setattr(socket, "init_ws_context", lambda _: ctx)
        monkeypatch.setattr(socket, "get_data_layer", lambda: None)
        monkeypatch.setattr("chainlit.message.get_data_layer", lambda: None)
        monkeypatch.setattr("chainlit.data.get_data_layer", lambda: None)
        monkeypatch.setattr(config.code, "author_rename", None)

        try:
            yield session, emitter, ctx
        finally:
            context_var.reset(token)


def _payload(**overrides):
    message = {
        "id": str(uuid.uuid4()),
        "type": "user_message",
        "output": "hello",
        "createdAt": "2024-01-01T00:00:00Z",
        "name": "User",
    }
    message.update(overrides)
    return {"message": message}


def _spans_named(exporter, name):
    return [span for span in exporter.get_finished_spans() if span.name == name]


# ---------------------------------------------------------------------------
# AC 1/5: disabled path
# ---------------------------------------------------------------------------


def _run_python(script: str) -> subprocess.CompletedProcess:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("OTEL_")
    }
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND_DIR),
        timeout=180,
    )


def test_import_without_telemetry_does_not_load_sdk():
    result = _run_python(
        "import sys\n"
        "import chainlit  # noqa: F401\n"
        "assert 'opentelemetry.sdk' not in sys.modules, sorted(\n"
        "    name for name in sys.modules if name.startswith('opentelemetry')\n"
        ")\n"
        "print('OK')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


@pytest.mark.parametrize(
    ("name", "value"),
    [("OTEL_SDK_DISABLED", "true"), ("OTEL_TRACES_EXPORTER", "none")],
)
def test_standard_env_can_force_the_disabled_path(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    _enable_config(monkeypatch)

    otel.init()

    assert otel._provider is None
    assert otel.start_turn("message", THREAD_ID) is None


def test_disabled_config_creates_no_provider():
    otel.init()

    assert otel._provider is None
    assert otel._tracer is None
    assert otel.start_turn("message", THREAD_ID) is None


def test_missing_extra_logs_once_and_noops(monkeypatch, caplog):
    _enable_config(monkeypatch)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.resources", None)

    with caplog.at_level(logging.WARNING, logger="chainlit"):
        otel.init()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if "otel" in record.getMessage() and "extra" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert otel._provider is None
    assert otel.start_turn("message", THREAD_ID) is None


# ---------------------------------------------------------------------------
# AC 2: one root per turn, steps as children
# ---------------------------------------------------------------------------


async def test_message_turn_exports_root_and_nested_step(
    monkeypatch, ws, memory_exporter
):
    import chainlit.socket as socket

    session, _emitter, _ctx = ws
    _init_with_exporter(monkeypatch, memory_exporter)

    @step_decorator(type="tool")
    async def search_docs(query: str):
        return f"result for {query}"

    async def on_message(message):
        await search_docs("q")

    on_message = AsyncMock(side_effect=on_message)
    monkeypatch.setattr(config.code, "on_message", on_message)

    await socket.process_message(session, _payload())

    spans = _finished(memory_exporter)
    roots = [span for span in spans if span.name == "chainlit.turn"]
    assert len(roots) == 1
    root = roots[0]
    assert root.attributes["chainlit.turn.kind"] == "message"
    assert root.attributes["chainlit.thread.id"] == THREAD_ID

    tools = [span for span in spans if span.name == "chainlit.tool"]
    assert len(tools) == 1
    assert tools[0].context.trace_id == root.context.trace_id
    assert tools[0].parent.span_id == root.context.span_id
    assert tools[0].attributes["chainlit.step.type"] == "tool"


async def test_siblings_parent_to_root_and_explicit_parent_id(
    monkeypatch, ws, memory_exporter
):
    _init_with_exporter(monkeypatch, memory_exporter)

    turn = otel.start_turn("message", THREAD_ID)
    assert turn is not None
    try:
        async with Step(name="first", type="tool") as first:
            pass
        async with Step(name="second", type="retrieval"):
            pass
        async with Step(name="explicit", type="llm", parent_id=first.id):
            pass
        with Step(name="sync", type="embedding"):
            pass
    finally:
        otel.end_turn(turn)

    spans = _finished(memory_exporter)
    root = next(span for span in spans if span.name == "chainlit.turn")
    by_name = {span.name: span for span in spans}

    assert by_name["chainlit.tool"].parent.span_id == root.context.span_id
    assert by_name["chainlit.retrieval"].parent.span_id == root.context.span_id
    assert by_name["chainlit.embedding"].parent.span_id == root.context.span_id
    assert (
        by_name["chainlit.llm"].parent.span_id
        == by_name["chainlit.tool"].context.span_id
    )
    for span in spans:
        assert span.context.trace_id == root.context.trace_id


async def test_edit_turn_has_the_same_root(monkeypatch, ws, memory_exporter):
    import chainlit.socket as socket

    session, _emitter, _ctx = ws
    _init_with_exporter(monkeypatch, memory_exporter)
    monkeypatch.setattr(socket, "chat_context", Mock(get=list))
    monkeypatch.setattr(config.code, "on_message", AsyncMock())

    await socket.process_edit_message(
        session, {"message": {"id": "m1", "output": "edited"}}
    )

    otel._provider.force_flush()
    roots = _spans_named(memory_exporter, "chainlit.turn")
    assert len(roots) == 1
    assert roots[0].attributes["chainlit.turn.kind"] == "edit_message"
    assert roots[0].attributes["chainlit.thread.id"] == THREAD_ID


# ---------------------------------------------------------------------------
# AC 3: redaction contract (primary)
# ---------------------------------------------------------------------------


async def test_step_content_never_reaches_the_exporter(
    monkeypatch, mock_chainlit_context, memory_exporter
):
    _init_with_exporter(monkeypatch, memory_exporter)

    async with mock_chainlit_context:
        step = Step(
            name=f"name-{SENTINEL}",
            type="tool",
            metadata={"secret": SENTINEL},
            tags=[SENTINEL],
            elements=[Text(name=SENTINEL, content=SENTINEL)],
        )
        step.input = SENTINEL
        step.output = SENTINEL
        step.generation = {"prompt": SENTINEL}

        turn = otel.start_turn("message", THREAD_ID)
        try:
            async with step:
                pass
        finally:
            otel.end_turn(turn)

    spans = _finished(memory_exporter)
    assert len(spans) == 2
    for span in spans:
        assert SENTINEL not in span.name
        assert SENTINEL not in json.dumps(span.attributes, default=str)
        assert span.events == ()


async def test_exported_attribute_keys_equal_the_allowlist(
    monkeypatch, mock_chainlit_context, memory_exporter
):
    _init_with_exporter(monkeypatch, memory_exporter)

    async with mock_chainlit_context:
        turn = otel.start_turn("message", THREAD_ID)
        try:
            async with Step(name="tool", type="tool"):
                pass
        finally:
            otel.end_turn(turn)

    spans = _finished(memory_exporter)
    root = next(span for span in spans if span.name == "chainlit.turn")
    step = next(span for span in spans if span.name == "chainlit.tool")

    assert set(root.attributes) == {"chainlit.turn.kind", "chainlit.thread.id"}
    assert set(step.attributes) == {
        "chainlit.step.type",
        "chainlit.step.is_error",
    }


async def test_exception_is_redacted_to_status_and_error_type(
    monkeypatch, mock_chainlit_context, memory_exporter
):
    from opentelemetry.trace import StatusCode

    _init_with_exporter(monkeypatch, memory_exporter)

    async def explode():
        async with Step(name="boom", type="tool"):
            raise RuntimeError(f"{SENTINEL} exploded")

    async with mock_chainlit_context:
        turn = otel.start_turn("message", THREAD_ID)
        try:
            with pytest.raises(RuntimeError):
                await explode()
        finally:
            otel.end_turn(turn)

    spans = _finished(memory_exporter)
    step = next(span for span in spans if span.name == "chainlit.tool")
    root = next(span for span in spans if span.name == "chainlit.turn")

    assert step.status.status_code == StatusCode.ERROR
    assert step.status.description == ""
    assert step.events == ()
    assert step.attributes["error.type"] == "RuntimeError"
    assert SENTINEL not in json.dumps(step.attributes, default=str)
    # The root only turns ERROR when the turn handler records the failure.
    assert "error.type" not in root.attributes


async def test_invalid_thread_id_is_omitted(monkeypatch, memory_exporter):
    _init_with_exporter(monkeypatch, memory_exporter)

    turn = otel.start_turn("message", "not-a-uuid\n")
    assert turn is not None
    otel.end_turn(turn)

    root = _finished(memory_exporter)[0]
    assert "chainlit.thread.id" not in root.attributes


def test_spans_from_other_instrumentations_are_not_exported(
    monkeypatch, memory_exporter
):
    _init_with_exporter(monkeypatch, memory_exporter)

    foreign = otel._provider.get_tracer("fastapi")
    span = foreign.start_span("GET /{full_path}")
    span.end()

    turn = otel.start_turn("message", THREAD_ID)
    assert turn is not None
    otel.end_turn(turn)

    assert [span.name for span in _finished(memory_exporter)] == ["chainlit.turn"]


# ---------------------------------------------------------------------------
# AC 4: failure isolation
# ---------------------------------------------------------------------------


def _raising_exporter():
    class RaisingExporter:
        def export(self, spans):
            raise RuntimeError("collector is down")

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    return RaisingExporter()


def _slow_exporter(block: threading.Event):
    class SlowExporter:
        def export(self, spans):
            block.wait(10)
            return None

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    return SlowExporter()


async def test_exporter_raising_does_not_change_the_turn(monkeypatch, ws, caplog):
    import chainlit.socket as socket

    session, emitter, _ctx = ws
    _init_with_exporter(monkeypatch, _raising_exporter())

    on_message = AsyncMock()
    monkeypatch.setattr(config.code, "on_message", on_message)

    with caplog.at_level(logging.WARNING):
        await socket.process_message(session, _payload())

    on_message.assert_awaited_once()
    # No error surfaced to the user and the turn finished normally.
    errors = [
        call.args[1]
        for call in emitter.session.emit.await_args_list
        if len(call.args) >= 2
        and call.args[0] == "new_message"
        and call.args[1].get("isError")
    ]
    assert errors == []


async def test_slow_exporter_does_not_change_the_turn(monkeypatch, ws):
    import chainlit.socket as socket

    session, _emitter, _ctx = ws
    block = threading.Event()
    _init_with_exporter(monkeypatch, _slow_exporter(block))

    on_message = AsyncMock()
    monkeypatch.setattr(config.code, "on_message", on_message)

    try:
        await asyncio.wait_for(socket.process_message(session, _payload()), timeout=5.0)
    finally:
        block.set()

    on_message.assert_awaited_once()


async def test_raising_tracer_does_not_change_the_turn(monkeypatch, ws):
    import chainlit.socket as socket

    session, _emitter, _ctx = ws
    _init_with_exporter(monkeypatch, Mock(export=lambda spans: None))

    class RaisingTracer:
        def start_span(self, *args, **kwargs):
            raise RuntimeError("tracer bug")

    otel._tracer = RaisingTracer()
    on_message = AsyncMock()
    monkeypatch.setattr(config.code, "on_message", on_message)

    await socket.process_message(session, _payload())

    on_message.assert_awaited_once()


async def test_app_error_still_sends_error_message_and_marks_the_root(
    monkeypatch, ws, memory_exporter
):
    from opentelemetry.trace import StatusCode

    import chainlit.socket as socket

    session, emitter, _ctx = ws
    _init_with_exporter(monkeypatch, memory_exporter)
    monkeypatch.setattr(
        config.code,
        "on_message",
        AsyncMock(side_effect=ValueError(f"{SENTINEL} app failure")),
    )

    await socket.process_message(session, _payload())

    payloads = [
        call.args[1]
        for call in emitter.session.emit.await_args_list
        if len(call.args) >= 2 and call.args[0] == "new_message"
    ]
    assert any(payload.get("isError") for payload in payloads)

    otel._provider.force_flush()
    roots = _spans_named(memory_exporter, "chainlit.turn")
    assert len(roots) == 1
    assert roots[0].status.status_code == StatusCode.ERROR
    assert roots[0].attributes["error.type"] == "ValueError"
    assert roots[0].status.description == ""


# ---------------------------------------------------------------------------
# AC 6: sampling and the span cap
# ---------------------------------------------------------------------------


def test_default_sampler_is_parent_based_ratio(monkeypatch, memory_exporter):
    from opentelemetry import context as otel_context
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    _init_with_exporter(monkeypatch, memory_exporter, sample_ratio=0.1)
    sampler = otel._provider.sampler

    assert isinstance(sampler, ParentBased)
    assert isinstance(sampler._root, TraceIdRatioBased)
    assert sampler._root._rate == 0.1

    empty = otel_context.Context()
    sampled = sampler.should_sample(empty, 0, "chainlit.turn")
    dropped = sampler.should_sample(empty, 2**64 - 1, "chainlit.turn")
    assert sampled.decision.is_sampled()
    assert not dropped.decision.is_sampled()


def test_standard_otlp_sampler_env_wins(monkeypatch, memory_exporter):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    _init_with_exporter(monkeypatch, memory_exporter, sample_ratio=0.0)

    from opentelemetry import context as otel_context

    assert otel._provider.sampler.should_sample(
        otel_context.Context(), 0, "chainlit.turn"
    ).decision.is_sampled()


async def test_span_cap_drops_and_counts(
    monkeypatch, mock_chainlit_context, memory_exporter
):
    _init_with_exporter(monkeypatch, memory_exporter)

    turn = otel.start_turn("message", THREAD_ID)
    assert turn is not None
    try:
        async with mock_chainlit_context:
            for index in range(otel.MAX_SPANS_PER_TURN + 5):
                async with Step(name=f"s{index}", type="tool"):
                    pass
    finally:
        otel.end_turn(turn)

    spans = _finished(memory_exporter)
    assert len(_spans_named(memory_exporter, "chainlit.tool")) == (
        otel.MAX_SPANS_PER_TURN
    )
    assert len(spans) == otel.MAX_SPANS_PER_TURN + 1
    assert turn.count == otel.MAX_SPANS_PER_TURN
    assert turn.dropped == 5
    root = next(span for span in spans if span.name == "chainlit.turn")
    assert root.attributes["chainlit.telemetry.dropped_spans"] == 5


async def test_unclosed_spans_are_closed_at_turn_end(
    monkeypatch, mock_chainlit_context, memory_exporter
):
    _init_with_exporter(monkeypatch, memory_exporter)

    turn = otel.start_turn("message", THREAD_ID)
    async with mock_chainlit_context:
        leaked = Step(name="leaky", type="tool")
        await leaked.__aenter__()
    otel.end_turn(turn)

    spans = _finished(memory_exporter)
    leaked_span = next(span for span in spans if span.name == "chainlit.tool")
    assert leaked_span.attributes["chainlit.span.unclosed"] is True
    root = next(span for span in spans if span.name == "chainlit.turn")
    assert root.end_time >= leaked_span.end_time


# ---------------------------------------------------------------------------
# AC 7: bounded shutdown and the lifespan hook
# ---------------------------------------------------------------------------


def test_shutdown_is_bounded(monkeypatch):
    class BlockingProvider:
        def __init__(self):
            self.calls = 0

        def shutdown(self):
            self.calls += 1
            threading.Event().wait(10)

    provider = BlockingProvider()
    otel._provider = provider
    otel._tracer = object()

    started = time.monotonic()
    otel.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 2.5
    assert provider.calls == 1
    assert otel._provider is None
    assert otel._tracer is None


async def test_lifespan_calls_otel_shutdown_before_exit(monkeypatch):
    order = []

    monkeypatch.setattr("chainlit.server.otel.shutdown", lambda: order.append("otel"))
    monkeypatch.setattr("chainlit.server.os._exit", lambda code: order.append("exit"))
    monkeypatch.setattr("chainlit.server._cleanup_files_directory", lambda: None)
    monkeypatch.setattr("chainlit.server.load_skills", lambda: None)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: None)
    monkeypatch.setattr(config.run, "headless", True)
    monkeypatch.setattr(config.run, "watch", False)
    monkeypatch.setattr(config.features, "prompt_scheduling", False)
    monkeypatch.setattr(config.code, "on_app_startup", None)
    monkeypatch.setattr(config.code, "on_app_shutdown", None)
    monkeypatch.setattr(config.features, "telemetry", TelemetryFeature(enabled=False))

    async with lifespan(chainlit_app):
        pass

    assert order == ["otel", "exit"]


def _blocking_exporter(entered: threading.Event, release: threading.Event):
    class BlockingExporter:
        def export(self, spans):
            entered.set()
            release.wait(10)
            return None

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    return BlockingExporter()


def test_shutdown_is_bounded_with_a_blocking_exporter(monkeypatch):
    """The SDK's own shutdown flush must not run on the exit path.

    A real ``TracerProvider`` + ``BatchSpanProcessor`` blocks inside
    ``provider.shutdown()`` until the exporter returns; the join budget is the
    only thing that keeps the process exit bounded.
    """
    entered = threading.Event()
    release = threading.Event()
    # The only export attempt must be the one shutdown triggers.
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "600000")
    _init_with_exporter(monkeypatch, _blocking_exporter(entered, release))

    otel.end_turn(otel.start_turn("message", THREAD_ID))
    assert not entered.is_set()

    started = time.monotonic()
    try:
        otel.shutdown()
        elapsed = time.monotonic() - started
        # The flush was reached, so the bound is a cut-short flush, not a no-op.
        assert entered.wait(5)
    finally:
        release.set()

    assert elapsed < 2.5
    assert otel._provider is None
    assert otel._tracer is None


def test_shutdown_never_touches_a_provider_chainlit_does_not_own(
    monkeypatch, memory_exporter
):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    app_provider = TracerProvider()
    app_provider.shutdown = Mock()
    trace.set_tracer_provider(app_provider)

    _init_with_exporter(monkeypatch, memory_exporter)
    otel.shutdown()

    app_provider.shutdown.assert_not_called()
    assert trace.get_tracer_provider() is app_provider


# ---------------------------------------------------------------------------
# AC 8: provider ownership (subprocesses: the global provider is sticky)
# ---------------------------------------------------------------------------


def test_chainlit_keeps_its_own_provider_when_app_has_one(tmp_path):
    script = f"""
import json, logging

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

records = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append((record.levelname, record.getMessage()))


logging.getLogger("chainlit").setLevel(logging.INFO)
logging.getLogger("chainlit").addHandler(Capture())

app_exporter = InMemorySpanExporter()
app_provider = TracerProvider()
app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
trace.set_tracer_provider(app_provider)
app_shutdowns = []
original_shutdown = app_provider.shutdown


def shutdown():
    app_shutdowns.append(True)
    original_shutdown()


app_provider.shutdown = shutdown

import chainlit.otel as otel
from chainlit.config import TelemetryFeature, config

config.features.telemetry.enabled = True
config.features.telemetry.sample_ratio = 1.0
chainlit_exporter = InMemorySpanExporter()
otel._build_exporter = lambda: (
    chainlit_exporter,
    "http://localhost:4318/v1/traces",
    [],
    0,
)
otel.init()

provider = otel._provider
turn = otel.start_turn("message", "{THREAD_ID}")
otel.end_turn(turn)
provider.force_flush()
otel.shutdown()

print(
    json.dumps(
        {{
            "chainlit_spans": len(chainlit_exporter.get_finished_spans()),
            "app_spans": len(app_exporter.get_finished_spans()),
            "app_is_global": trace.get_tracer_provider() is app_provider,
            "app_shutdowns": len(app_shutdowns),
            "info_lines": [
                message
                for level, message in records
                if level == "INFO" and "app provider was not modified" in message
            ],
        }}
    )
)
"""
    result = _run_python(script)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])

    assert data["chainlit_spans"] == 1
    assert data["app_spans"] == 0
    assert data["app_is_global"] is True
    assert data["app_shutdowns"] == 0
    assert len(data["info_lines"]) == 1


def test_chainlit_isolation_holds_when_the_app_exporter_blocks():
    """A blocking app exporter must never see a Chainlit span or a shutdown.

    ``SimpleSpanProcessor`` exports inline on span end, so a turn that leaked
    onto the app provider would stall here; a ``shutdown()`` call to that
    provider would stall the lifespan instead.
    """
    script = f"""
import json, time

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

app_exports = []
app_shutdowns = []


class BlockingExporter:
    def export(self, spans):
        app_exports.append(len(spans))
        time.sleep(5)
        return None

    def shutdown(self):
        app_shutdowns.append(True)

    def force_flush(self, timeout_millis=30000):
        return True


app_provider = TracerProvider()
app_provider.add_span_processor(SimpleSpanProcessor(BlockingExporter()))
trace.set_tracer_provider(app_provider)

import chainlit.otel as otel
from chainlit.config import TelemetryFeature, config

config.features.telemetry = TelemetryFeature(enabled=True, sample_ratio=1.0)
chainlit_exporter = InMemorySpanExporter()
otel._build_exporter = lambda: (
    chainlit_exporter,
    "http://localhost:4318/v1/traces",
    [],
    0,
)
otel.init()

started = time.monotonic()
otel.end_turn(otel.start_turn("message", "{THREAD_ID}"))
turn_elapsed = time.monotonic() - started
started = time.monotonic()
otel.shutdown()
shutdown_elapsed = time.monotonic() - started

print(
    json.dumps(
        {{
            "chainlit_spans": len(chainlit_exporter.get_finished_spans()),
            "app_exports": app_exports,
            "app_shutdowns": len(app_shutdowns),
            "app_is_global": trace.get_tracer_provider() is app_provider,
            "turn_elapsed": turn_elapsed,
            "shutdown_elapsed": shutdown_elapsed,
        }}
    )
)
"""
    result = _run_python(script)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])

    assert data["turn_elapsed"] < 2.0
    assert data["shutdown_elapsed"] < 2.5
    assert data["app_exports"] == []
    assert data["app_shutdowns"] == 0
    assert data["app_is_global"] is True
    assert data["chainlit_spans"] == 1


def test_chainlit_installs_its_provider_as_global_by_default():
    script = """
import json

from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

import chainlit.otel as otel
from chainlit.config import TelemetryFeature, config

config.features.telemetry.enabled = True
config.features.telemetry.sample_ratio = 1.0
otel._build_exporter = lambda: (
    InMemorySpanExporter(),
    "http://localhost:4318/v1/traces",
    [],
    0,
)
otel.init()

print(json.dumps({"is_global": trace.get_tracer_provider() is otel._provider}))
"""
    result = _run_python(script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["is_global"] is True


# ---------------------------------------------------------------------------
# AC 9: no ambient context, no baggage
# ---------------------------------------------------------------------------


async def test_ambient_context_and_baggage_are_ignored(
    monkeypatch, ws, memory_exporter
):
    from opentelemetry import baggage, context as otel_context, trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

    import chainlit.socket as socket

    session, _emitter, _ctx = ws
    _init_with_exporter(monkeypatch, memory_exporter)

    async def on_message(message):
        async with Step(name="tool", type="tool"):
            pass

    monkeypatch.setattr(config.code, "on_message", AsyncMock(side_effect=on_message))

    remote = SpanContext(
        trace_id=0x1234567890ABCDEF1234567890ABCDEF,
        span_id=0x1234567890ABCDEF,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    remote_span = NonRecordingSpan(remote)
    token = otel_context.attach(
        trace.set_span_in_context(remote_span, otel_context.Context())
    )
    baggage_token = otel_context.attach(
        baggage.set_baggage(f"chainlit.test.{SENTINEL}", SENTINEL)
    )
    try:
        await socket.process_message(session, _payload())
    finally:
        otel_context.detach(baggage_token)
        otel_context.detach(token)

    spans = _finished(memory_exporter)
    root = next(span for span in spans if span.name == "chainlit.turn")
    assert root.context.trace_id != remote.trace_id
    assert root.parent is None
    assert any(span.name == "chainlit.tool" for span in spans)
    for span in spans:
        assert span.context.trace_id == root.context.trace_id
        if span.parent is not None:
            assert span.parent.trace_id == root.context.trace_id
        assert SENTINEL not in span.name
        assert SENTINEL not in json.dumps(span.attributes, default=str)
        assert SENTINEL not in json.dumps([dict(event) for event in span.events])


# ---------------------------------------------------------------------------
# AC 10: OTLP headers and endpoint never leak into logs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_var",
    ["OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"],
)
def test_header_secrets_never_reach_logs(monkeypatch, caplog, env_var):
    raw = ",".join(
        [
            f"not-a-header-{SENTINEL}",
            "x-api-key=abc123",
            f"x-bad-newline={SENTINEL}\nINJECTED",
            f"x-bad-encoded={SENTINEL}%0AINJECTED",
            f"x-bad-del={SENTINEL}\x7f",
            f"x-bad-unicode={SENTINEL}\u00e9",
            f"x-bad-name-{SENTINEL}\n=value",
        ]
    )
    monkeypatch.setenv(env_var, raw)
    _enable_config(monkeypatch)

    captured = {}

    class FakeExporter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def export(self, spans):
            return None

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    monkeypatch.setattr(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
        FakeExporter,
    )

    with (
        caplog.at_level(logging.DEBUG, logger="chainlit"),
        caplog.at_level(logging.DEBUG, logger="opentelemetry"),
    ):
        otel.init()

    assert otel._provider is not None
    assert captured["headers"] == {"x-api-key": "abc123"}
    assert SENTINEL not in caplog.text
    assert "dropped 6 malformed OTLP header entries" in caplog.text
    assert "x-api-key" in caplog.text
    # The env vars are restored after construction.
    assert os.environ[env_var] == raw


def test_malformed_resource_attributes_are_not_logged(
    monkeypatch, caplog, memory_exporter
):
    raw = f"badpair-{SENTINEL},service.namespace=chainlit"
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", raw)
    with caplog.at_level(logging.DEBUG, logger="opentelemetry"):
        _init_with_exporter(monkeypatch, memory_exporter)

    assert SENTINEL not in caplog.text
    assert SENTINEL not in json.dumps(
        dict(otel._provider.resource.attributes), default=str
    )
    assert otel._provider.resource.attributes["service.namespace"] == "chainlit"
    assert os.environ["OTEL_RESOURCE_ATTRIBUTES"] == raw


@pytest.mark.parametrize(
    "endpoint",
    [
        f"https://user:{SENTINEL}@collector.example:4318/v1/traces",
        f"https://collector.example:4318/v1/traces?token={SENTINEL}",
        f"https://collector.example:4318/v1/traces#{SENTINEL}",
        f"https://collector.example:4318/v1/traces;{SENTINEL}",
    ],
)
def test_endpoint_credentials_or_query_is_rejected(monkeypatch, caplog, endpoint):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    _enable_config(monkeypatch)

    with (
        caplog.at_level(logging.DEBUG, logger="chainlit"),
        caplog.at_level(logging.DEBUG, logger="opentelemetry"),
    ):
        otel.init()

    assert otel._provider is None
    assert SENTINEL not in caplog.text
    assert "invalid OTLP endpoint" not in caplog.text


def test_endpoint_path_secret_never_reaches_exporter_logs(monkeypatch, caplog):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    endpoint = f"http://127.0.0.1:9/v1/traces/{SENTINEL}"
    _enable_config(monkeypatch)
    monkeypatch.setattr(
        otel,
        "_build_exporter",
        lambda: (OTLPSpanExporter(endpoint=endpoint, timeout=2.0), endpoint, [], 0),
    )

    with (
        caplog.at_level(logging.DEBUG, logger="chainlit"),
        caplog.at_level(logging.DEBUG, logger="opentelemetry"),
        caplog.at_level(logging.DEBUG, logger="urllib3"),
        caplog.at_level(logging.DEBUG, logger="requests"),
    ):
        otel.init()
        turn = otel.start_turn("message", THREAD_ID)
        assert turn is not None
        otel.end_turn(turn)
        otel._provider.force_flush()

    assert "Transient error" in caplog.text
    assert SENTINEL not in caplog.text
    assert "/v1/traces" not in caplog.text
    assert "http://127.0.0.1:9" in caplog.text


def test_malformed_endpoint_is_not_echoed(monkeypatch, caplog):
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        f"http://localhost:{SENTINEL}/v1/traces",
    )
    _enable_config(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="chainlit"):
        otel.init()

    assert otel._provider is None
    assert SENTINEL not in caplog.text


# ---------------------------------------------------------------------------
# AC 11: error.type sanitizer
# ---------------------------------------------------------------------------


def test_error_type_sanitizer_bounds_dynamic_class_names():
    pattern = r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$"
    import re

    assert otel._sanitize_error_type(ValueError) == "ValueError"
    assert otel._sanitize_error_type(KeyError) == "KeyError"
    assert otel._sanitize_error_type(None) == "error"
    for name in ("bad name", "bad\nname", "\x1b[31mfake", "0digit", "x" * 200):
        dynamic = type(name, (Exception,), {})
        sanitized = otel._sanitize_error_type(dynamic)
        assert re.fullmatch(pattern, sanitized), (name, sanitized)
        assert sanitized == "error"


# ---------------------------------------------------------------------------
# AC 12: queue-full log rate
# ---------------------------------------------------------------------------


async def test_queue_full_logs_at_most_twice(
    monkeypatch, caplog, mock_chainlit_context
):
    block = threading.Event()

    class BlockingExporter:
        def export(self, spans):
            block.wait(30)

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "1")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
    _init_with_exporter(monkeypatch, BlockingExporter())

    try:
        with caplog.at_level(logging.WARNING):
            turn = otel.start_turn("message", THREAD_ID)
            try:
                async with mock_chainlit_context:
                    for index in range(10):
                        async with Step(name=f"s{index}", type="tool"):
                            pass
            finally:
                otel.end_turn(turn)
    finally:
        block.set()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if "queue" in record.getMessage().lower()
        and "full" in record.getMessage().lower()
    ]
    assert 1 <= len(warnings) <= 2
    assert turn is not None
