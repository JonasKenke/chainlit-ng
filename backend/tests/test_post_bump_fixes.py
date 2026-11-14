"""Regression tests for fixes landed after the 2.12.0 release."""

import importlib
import warnings
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import chainlit
import chainlit.data as data_module
from chainlit.config import config as app_config
from chainlit.element import Element
from chainlit.step import Step

step_module = importlib.import_module("chainlit.step")


@pytest.fixture
def isolated_data_layer(monkeypatch: pytest.MonkeyPatch):
    """Reset the data-layer module globals and the configured data layer."""
    monkeypatch.setattr(data_module, "_data_layer", None)
    monkeypatch.setattr(data_module, "_data_layer_initialized", False)
    monkeypatch.setattr(app_config.code, "data_layer", None)
    monkeypatch.delenv("LITERAL_API_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return data_module


class TestLiteralApiKeyWarning:
    def test_warns_when_no_data_layer_is_configured(
        self, isolated_data_layer, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("LITERAL_API_KEY", "literal-key")

        with pytest.warns(UserWarning, match="LITERAL_API_KEY"):
            layer = isolated_data_layer.get_data_layer()

        assert layer is None

    def test_does_not_warn_when_a_data_layer_is_configured(
        self, isolated_data_layer, monkeypatch: pytest.MonkeyPatch
    ):
        sentinel = object()
        monkeypatch.setenv("LITERAL_API_KEY", "literal-key")
        monkeypatch.setattr(app_config.code, "data_layer", lambda: sentinel)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            layer = isolated_data_layer.get_data_layer()

        assert layer is sentinel
        assert not any("LITERAL_API_KEY" in str(w.message) for w in caught)


class TestLazyModuleAttributes:
    def test_unknown_attribute_raises_attribute_error(self):
        assert hasattr(chainlit, "definitely_missing") is False
        with pytest.raises(AttributeError):
            getattr(chainlit, "definitely_missing")

    def test_known_lazy_export_still_resolves(self):
        handler = chainlit.LangchainCallbackHandler

        assert handler.__name__ == "LangchainTracer"
        assert chainlit.__all__
        assert "LangchainCallbackHandler" in chainlit.__all__


class TestGenerationSerialization:
    async def test_generation_to_dict_is_used_when_persisting(
        self, mock_chainlit_context, monkeypatch: pytest.MonkeyPatch
    ):
        class FakeGeneration:
            def to_dict(self):
                return {"type": "CHAT", "token": "value"}

        data_layer = Mock()
        data_layer.update_step = AsyncMock()
        monkeypatch.setattr(step_module, "get_data_layer", lambda: data_layer)

        async with mock_chainlit_context:
            step = Step(name="llm-step", type="llm")
            step.generation = cast(Any, FakeGeneration())

            await step.update()
            await step.flush_persistence()

        data_layer.update_step.assert_awaited_once()
        persisted = data_layer.update_step.await_args.args[0]
        assert persisted["generation"] == {"type": "CHAT", "token": "value"}


class TestBrowserRenderedMime:
    @pytest.mark.parametrize(
        "mime",
        [
            "image/png",
            "image/webp",
            "application/pdf",
            "audio/mpeg",
            "video/mp4",
            "IMAGE/PNG; charset=binary",
        ],
    )
    def test_rendered_types_are_inline(self, mime: str):
        assert Element.is_browser_rendered_mime(mime) is True

    @pytest.mark.parametrize(
        "mime",
        [
            "text/plain",
            "application/zip",
            "application/octet-stream",
            "image/svg+xml",
            "not-a-mime",
            "",
        ],
    )
    def test_non_rendered_types_are_attachment(self, mime: str):
        assert Element.is_browser_rendered_mime(mime) is False
