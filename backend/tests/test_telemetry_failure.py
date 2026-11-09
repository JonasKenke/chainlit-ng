"""Slice 3 of the KRE-338 design: failure injection and bound proof."""

import asyncio
import gc
import importlib
import logging
import threading
import time
import uuid
import weakref
from unittest.mock import AsyncMock, Mock

import pytest

import chainlit.otel as otel
from chainlit.config import TelemetryFeature, config
from chainlit.context import ChainlitContext, context_var
from chainlit.emitter import ChainlitEmitter
from chainlit.server import app as chainlit_app, lifespan
from chainlit.step import Step

THREAD_ID = "3b0f7f7e-2f1f-4a7b-9c5e-9f1f2a3b4c5d"

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


def _enable_config(
    monkeypatch, sample_ratio: float = 1.0, capture_names: bool = False
) -> None:
    monkeypatch.setattr(
        config.features,
        "telemetry",
        TelemetryFeature(
            enabled=True,
            capture_names=capture_names,
            sample_ratio=sample_ratio,
        ),
    )


def _init_with_exporter(
    monkeypatch, exporter, sample_ratio: float = 1.0, capture_names: bool = False
):
    """Initialize telemetry with an in-memory exporter instead of OTLP."""
    _enable_config(monkeypatch, sample_ratio=sample_ratio, capture_names=capture_names)
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


def _emit_errors(emitter):
    return [
        call.args[1]
        for call in emitter.session.emit.await_args_list
        if len(call.args) >= 2
        and call.args[0] == "new_message"
        and call.args[1].get("isError")
    ]


async def _run_turn(monkeypatch, ws):
    import chainlit.socket as socket

    session, emitter, _ctx = ws
    on_message = AsyncMock()
    monkeypatch.setattr(config.code, "on_message", on_message)

    await asyncio.wait_for(socket.process_message(session, _payload()), timeout=10.0)

    on_message.assert_awaited_once()
    assert _emit_errors(emitter) == []
    return emitter


def _records(caplog, *needles):
    return [
        record
        for record in caplog.records
        if all(needle in record.getMessage().lower() for needle in needles)
    ]


# ---------------------------------------------------------------------------
# Failure-injection matrix (§11 slice 3 AC 1)
# ---------------------------------------------------------------------------


async def test_provider_init_raising_completes_the_turn_and_warns_once(
    monkeypatch, ws, caplog
):
    _enable_config(monkeypatch)
    monkeypatch.setattr(
        otel,
        "_build_exporter",
        lambda: (Mock(), "http://localhost:4318/v1/traces", [], 0),
    )

    class RaisingProvider:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("provider init failed")

    monkeypatch.setattr("opentelemetry.sdk.trace.TracerProvider", RaisingProvider)

    with caplog.at_level(logging.WARNING):
        otel.init()
        await _run_turn(monkeypatch, ws)

    assert otel._provider is None
    assert otel._tracer is None
    records = _records(caplog, "provider initialization failed")
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


async def test_tracer_raising_completes_the_turn_and_warns_once(
    monkeypatch, ws, memory_exporter, caplog
):
    _init_with_exporter(monkeypatch, memory_exporter)

    class RaisingTracer:
        def start_span(self, *args, **kwargs):
            raise RuntimeError("tracer bug")

    otel._tracer = RaisingTracer()

    with caplog.at_level(logging.WARNING):
        await _run_turn(monkeypatch, ws)

    records = _records(caplog, "failed to start a turn span")
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


async def test_exporter_raising_completes_the_turn_and_warns_once(
    monkeypatch, ws, caplog
):
    class RaisingExporter:
        def export(self, spans):
            raise RuntimeError("collector is down")

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    _init_with_exporter(monkeypatch, RaisingExporter())

    with caplog.at_level(logging.WARNING):
        await _run_turn(monkeypatch, ws)
        otel._provider.force_flush()

    records = _records(caplog, "exception while exporting")
    assert len(records) == 1


async def test_queue_full_completes_the_turn_and_warns_once(monkeypatch, ws, caplog):
    entered = threading.Event()
    release = threading.Event()

    class BlockingExporter:
        def export(self, spans):
            entered.set()
            release.wait(10)
            return None

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "1")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "600000")
    _init_with_exporter(monkeypatch, BlockingExporter())

    async def on_message(message):
        async with Step(name="first", type="tool"):
            pass
        # The first span fills the queue and blocks the worker in export.
        assert entered.wait(5)
        for index in range(10):
            async with Step(name=f"s{index}", type="tool"):
                pass

    import chainlit.socket as socket

    session, emitter, _ctx = ws
    monkeypatch.setattr(config.code, "on_message", AsyncMock(side_effect=on_message))

    try:
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(
                socket.process_message(session, _payload()), timeout=10.0
            )
    finally:
        release.set()

    assert _emit_errors(emitter) == []
    records = _records(caplog, "queue full")
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


async def test_slow_export_completes_the_turn_without_waiting(monkeypatch, ws, caplog):
    entered = threading.Event()
    release = threading.Event()

    class SlowExporter:
        def export(self, spans):
            entered.set()
            release.wait(10)
            return None

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "1")
    _init_with_exporter(monkeypatch, SlowExporter())

    async def on_message(message):
        async with Step(name="first", type="tool"):
            pass
        assert entered.wait(5)
        async with Step(name="second", type="tool"):
            pass

    import chainlit.socket as socket

    session, emitter, _ctx = ws
    monkeypatch.setattr(config.code, "on_message", AsyncMock(side_effect=on_message))

    try:
        with caplog.at_level(logging.WARNING):
            await asyncio.wait_for(
                socket.process_message(session, _payload()), timeout=5.0
            )
    finally:
        release.set()

    assert _emit_errors(emitter) == []
    assert len(_records(caplog, "exception while exporting")) == 0


async def test_app_owned_provider_present_completes_the_turn(
    monkeypatch, ws, memory_exporter, caplog
):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    app_exporter = InMemorySpanExporter()
    app_provider = TracerProvider()
    app_provider.add_span_processor(SimpleSpanProcessor(app_exporter))
    app_provider.shutdown = Mock()
    trace.set_tracer_provider(app_provider)

    with caplog.at_level(logging.INFO):
        _init_with_exporter(monkeypatch, memory_exporter)
        await _run_turn(monkeypatch, ws)
        otel._provider.force_flush()
        otel.shutdown()

    assert trace.get_tracer_provider() is app_provider
    assert app_exporter.get_finished_spans() == ()
    assert len(memory_exporter.get_finished_spans()) == 1
    app_provider.shutdown.assert_not_called()
    assert len(_records(caplog, "app provider was not modified")) == 1
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# Bound proof (§11 slice 3 AC 2 and AC 3)
# ---------------------------------------------------------------------------


async def test_thousand_step_turn_exports_the_cap_and_drops_the_rest(
    monkeypatch, mock_chainlit_context, memory_exporter
):
    _init_with_exporter(monkeypatch, memory_exporter)

    async with mock_chainlit_context:
        turn = otel.start_turn("message", THREAD_ID)
        assert turn is not None
        reference = weakref.ref(turn)
        for index in range(1000):
            async with Step(name=f"s{index}", type="tool"):
                pass
        otel.end_turn(turn)

    spans = _finished(memory_exporter)
    step_spans = [span for span in spans if span.name == "chainlit.tool"]
    root = next(span for span in spans if span.name == "chainlit.turn")

    assert len(step_spans) == otel.MAX_SPANS_PER_TURN == 200
    # The root is not counted against the step cap.
    assert len(spans) == 201
    assert root.attributes["chainlit.telemetry.dropped_spans"] == 800
    assert turn.count == 200
    assert turn.dropped == 800

    # The per-turn registry releases its handles after the turn.
    assert turn.spans == {}
    assert otel._turn.get() is None
    del turn
    gc.collect()
    assert reference() is None


async def test_exported_spans_respect_the_attribute_and_payload_bounds(
    monkeypatch, mock_chainlit_context, memory_exporter
):
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "deployment.environment=test,service.namespace=chainlit-tests",
    )
    _init_with_exporter(monkeypatch, memory_exporter, capture_names=True)

    long_name = "x" * 300
    async with mock_chainlit_context:
        turn = otel.start_turn("message", THREAD_ID)
        assert turn is not None
        try:
            async with Step(name=long_name, type="tool"):
                pass
            with pytest.raises(RuntimeError):
                async with Step(name="explode", type="tool"):
                    raise RuntimeError("boom")
            leaked = Step(name="leaked", type="tool")
            await leaked.__aenter__()
            otel.mark_turn_error(turn, ValueError)
            otel.mark_turn_cancelled(turn)
        finally:
            otel.end_turn(turn)

    spans = _finished(memory_exporter)
    root = next(span for span in spans if span.name == "chainlit.turn")
    step_names = [span.name for span in spans if span is not root]
    assert sorted(step_names) == sorted(["x" * 128, "explode", "leaked"])
    for span in spans:
        assert len(span.attributes) <= 8
        for key, value in span.attributes.items():
            assert not isinstance(value, (dict, list, tuple, set)), key
            assert isinstance(value, (str, bool, int, float)), key
            if isinstance(value, str):
                assert len(value) <= 128, key
        assert span.events == ()
        assert span.links == ()

        # Resource attributes live outside the per-span attribute bound.
        assert "service.name" in span.resource.attributes
        assert span.resource.attributes["deployment.environment"] == "test"
        assert "service.name" not in span.attributes
        assert "deployment.environment" not in span.attributes


# ---------------------------------------------------------------------------
# Shutdown wall-clock proof in the lifespan path (§11 slice 3 AC 4)
# ---------------------------------------------------------------------------


async def test_lifespan_shutdown_with_a_blocking_exporter_is_bounded(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class BlockingExporter:
        def export(self, spans):
            entered.set()
            release.wait(10)
            return None

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "1")
    _init_with_exporter(monkeypatch, BlockingExporter())
    otel.end_turn(otel.start_turn("message", THREAD_ID))
    assert entered.wait(5)

    exits = []
    monkeypatch.setattr("chainlit.server.os._exit", lambda code: exits.append(code))
    monkeypatch.setattr("chainlit.server._cleanup_files_directory", lambda: None)
    monkeypatch.setattr("chainlit.server.load_skills", lambda: None)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: None)
    monkeypatch.setattr(config.run, "headless", True)
    monkeypatch.setattr(config.run, "watch", False)
    monkeypatch.setattr(config.features, "prompt_scheduling", False)
    monkeypatch.setattr(config.code, "on_app_startup", None)
    monkeypatch.setattr(config.code, "on_app_shutdown", None)

    started = time.monotonic()
    try:
        async with lifespan(chainlit_app):
            pass
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert elapsed < 2.5
    assert exits == [0]
