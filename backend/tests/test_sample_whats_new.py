"""Tests for the ``whats_new_after_2_12`` demo app.

The sample registers ``chainlit.config.code`` callbacks and sets
``CHAINLIT_AUTH_SECRET`` when imported. Import it under a fixture that restores
both so the rest of the backend suite is unaffected.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from chainlit import config as chainlit_config

SAMPLE_DIR = (
    Path(__file__).resolve().parents[1] / "chainlit" / "sample" / "whats_new_after_2_12"
)
APP_PATH = SAMPLE_DIR / "app.py"
MODULE_NAME = "chainlit_sample_whats_new_after_2_12"

CODE_CALLBACKS = (
    "on_chat_start",
    "on_message",
    "password_auth_callback",
    "set_starters",
)


class RecordingMessage:
    """Minimal ``cl.Message`` stand-in that records sends."""

    sent: list["RecordingMessage"] = []

    def __init__(self, content: str = "", **_kwargs) -> None:
        self.content = content

    async def send(self) -> None:
        type(self).sent.append(self)


@pytest.fixture(scope="module")
def demo():
    saved_code = {
        name: getattr(chainlit_config.config.code, name) for name in CODE_CALLBACKS
    }
    had_secret = "CHAINLIT_AUTH_SECRET" in os.environ
    saved_secret = os.environ.get("CHAINLIT_AUTH_SECRET")

    spec = importlib.util.spec_from_file_location(MODULE_NAME, APP_PATH)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, value in saved_code.items():
            setattr(chainlit_config.config.code, name, value)
        if had_secret:
            assert saved_secret is not None
            os.environ["CHAINLIT_AUTH_SECRET"] = saved_secret
        else:
            os.environ.pop("CHAINLIT_AUTH_SECRET", None)
        sys.modules.pop(MODULE_NAME, None)


@pytest.fixture
def recording_messages(demo, monkeypatch):
    RecordingMessage.sent = []
    monkeypatch.setattr(demo.cl, "Message", RecordingMessage)
    return RecordingMessage.sent


@pytest.fixture
def clear_cards_cache(demo):
    demo._built_ui_has_cards.cache_clear()
    yield
    demo._built_ui_has_cards.cache_clear()


def _write_build(root: Path, bundle: str) -> None:
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        '<script type="module" src="assets/index-demo.js"></script>',
        encoding="utf-8",
    )
    (root / "assets" / "index-demo.js").write_text(bundle, encoding="utf-8")


def test_binary_png_is_valid_96x64_rgb(demo):
    data = demo.PNG_BYTES

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    assert int.from_bytes(data[16:20], "big") == 96
    assert int.from_bytes(data[20:24], "big") == 64
    assert data[24] == 8  # bit depth
    assert data[25] == 2  # colour type: truecolour RGB
    assert data.endswith(b"IEND\xaeB`\x82")


def test_demo_secret_is_long_enough_for_pyjwt(demo):
    assert len(demo.DEMO_AUTH_SECRET.encode()) >= 32


def test_built_ui_has_cards_true(demo, tmp_path, monkeypatch, clear_cards_cache):
    from chainlit import server

    _write_build(
        tmp_path,
        "approval-card question-card Save & approve Request changes",
    )
    monkeypatch.setattr(server, "build_dir", str(tmp_path))

    assert demo._built_ui_has_cards() is True


def test_built_ui_has_cards_false_when_edit_markers_missing(
    demo, tmp_path, monkeypatch, clear_cards_cache
):
    from chainlit import server

    # The old check accepted this bundle; the edit/request-changes actions
    # prove the generated-edit UI is present.
    _write_build(tmp_path, "approval-card question-card")
    monkeypatch.setattr(server, "build_dir", str(tmp_path))

    assert demo._built_ui_has_cards() is False


def test_release_notes_extraction(demo):
    notes = demo._release_notes()

    assert notes
    assert notes.startswith("### Changed")
    assert "## [2.12.0]" not in notes
    assert "CHANGELOG.md is not reachable" not in notes


async def test_on_chat_start_sends_no_message(demo, monkeypatch, recording_messages):
    monkeypatch.setattr(demo, "_built_ui_has_cards", lambda: True)

    await demo.on_chat_start()

    assert recording_messages == []


async def test_on_chat_start_stale_bundle_is_console_only(
    demo, monkeypatch, recording_messages, capsys
):
    monkeypatch.setattr(demo, "_built_ui_has_cards", lambda: False)

    await demo.on_chat_start()

    assert recording_messages == []
    assert "stale" in capsys.readouterr().out.lower()


async def test_starters_register_and_send_no_message(demo, recording_messages):
    starters = await demo.starters(None)

    assert [starter.message for starter in starters] == [
        "approval",
        "ticket",
        "markdown",
        "table",
        "ask",
        "media",
        "changes",
    ]
    assert chainlit_config.config.code.set_starters is not None
    assert recording_messages == []


async def test_latex_reports_the_configured_value(
    demo, monkeypatch, recording_messages
):
    monkeypatch.setattr(chainlit_config.config.features, "latex", False)
    await demo.demo_latex()
    assert "disabled" in recording_messages[-1].content
    assert "latex = true" in recording_messages[-1].content

    monkeypatch.setattr(chainlit_config.config.features, "latex", True)
    await demo.demo_latex()
    assert "enabled" in recording_messages[-1].content
    assert "disabled" not in recording_messages[-1].content


def test_email_preview_is_a_markdown_table_with_redacted_key(demo):
    preview = demo.EMAIL_PREVIEW

    assert "| field | value |" in preview
    assert "| API key | `[redacted]` |" in preview
    assert "sk-demo" not in preview


async def test_help_command_sends_the_table(demo, recording_messages):
    assert "help" in demo.COMMANDS

    await demo.COMMANDS["help"]()

    assert recording_messages[-1].content == demo.HELP
