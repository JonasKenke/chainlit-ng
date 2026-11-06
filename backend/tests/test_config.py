import json
from pathlib import Path

import pytest

from chainlit import config as chainlit_config
from chainlit.config import DEFAULT_CONFIG_STR, ChainlitConfig, UISettings


class TestLoadSettingsMetaGuard:
    def test_meta_section_without_generated_by_raises_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('[meta]\nsome_other_key = "value"\n', encoding="utf-8")
        monkeypatch.setattr(chainlit_config, "config_file", str(cfg_file))

        with pytest.raises(ValueError, match="outdated"):
            chainlit_config.load_settings()


@pytest.fixture
def translation_dir(tmp_path: Path) -> Path:
    """Minimal translation directory with a controlled set of locale files."""
    t_dir = tmp_path / "translations"
    t_dir.mkdir()

    files: dict[str, dict] = {
        "en-US.json": {"greeting": "Hello"},
        "es.json": {"greeting": "Hola"},
        "da-DK.json": {"greeting": "Hej"},
        "de-DE.json": {"greeting": "Hallo"},
        "zh-CN.json": {"greeting": "你好 CN"},
        "zh-TW.json": {"greeting": "你好 TW"},
    }
    for filename, content in files.items():
        (t_dir / filename).write_text(json.dumps(content), encoding="utf-8")

    return t_dir


class TestLoadTranslation:
    """Regression tests for the load_translation fallback chain."""

    def test_exact_match_regional(
        self,
        test_config: ChainlitConfig,
        translation_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Exact regional locale (da-DK) resolves directly to its file."""
        monkeypatch.setattr(
            chainlit_config, "config_translation_dir", str(translation_dir)
        )
        assert test_config.load_translation("da-DK") == {"greeting": "Hej"}

    def test_exact_match_base(
        self,
        test_config: ChainlitConfig,
        translation_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Exact base locale (es) resolves directly to its file."""
        monkeypatch.setattr(
            chainlit_config, "config_translation_dir", str(translation_dir)
        )
        assert test_config.load_translation("es") == {"greeting": "Hola"}

    def test_parent_fallback(
        self,
        test_config: ChainlitConfig,
        translation_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Regional locale (es-419) falls back to base file (es.json) when no exact match."""
        monkeypatch.setattr(
            chainlit_config, "config_translation_dir", str(translation_dir)
        )
        assert test_config.load_translation("es-419") == {"greeting": "Hola"}

    def test_regional_variant_lookup(
        self,
        test_config: ChainlitConfig,
        translation_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Base locale (da) resolves to regional file (da-DK.json) when no exact match exists."""
        monkeypatch.setattr(
            chainlit_config, "config_translation_dir", str(translation_dir)
        )
        assert test_config.load_translation("da") == {"greeting": "Hej"}

    def test_regional_variant_lookup_de(
        self,
        test_config: ChainlitConfig,
        translation_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Base locale (de) resolves to regional file (de-DE.json) via variant lookup."""
        monkeypatch.setattr(
            chainlit_config, "config_translation_dir", str(translation_dir)
        )
        assert test_config.load_translation("de") == {"greeting": "Hallo"}

    def test_regional_variant_sorted_deterministic(
        self,
        test_config: ChainlitConfig,
        translation_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """When multiple regional variants exist, the first sorted match (zh-CN) is returned."""
        monkeypatch.setattr(
            chainlit_config, "config_translation_dir", str(translation_dir)
        )
        assert test_config.load_translation("zh") == {"greeting": "你好 CN"}

    def test_default_fallback_unknown_locale(
        self,
        test_config: ChainlitConfig,
        translation_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Completely unknown locale (xx) falls back to en-US."""
        monkeypatch.setattr(
            chainlit_config, "config_translation_dir", str(translation_dir)
        )
        assert test_config.load_translation("xx") == {"greeting": "Hello"}

    def test_default_fallback_base_without_regional_variant(
        self,
        test_config: ChainlitConfig,
        translation_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Base locale (fr) with no matching file at all falls back to en-US."""
        monkeypatch.setattr(
            chainlit_config, "config_translation_dir", str(translation_dir)
        )
        assert test_config.load_translation("fr") == {"greeting": "Hello"}


class TestUISettings:
    def test_cot_display_defaults_to_list(self):
        assert UISettings(name="Assistant").cot_display == "list"

    def test_cot_display_accepts_compact(self):
        assert (
            UISettings(name="Assistant", cot_display="compact").cot_display == "compact"
        )

    def test_config_template_documents_cot_display(self):
        assert '# cot_display = "list"' in DEFAULT_CONFIG_STR


class TestCompactStepsTranslations:
    def test_all_locales_define_compact_step_labels(self):
        files = sorted(Path(chainlit_config.TRANSLATIONS_DIR).glob("*.json"))
        assert files

        for file in files:
            status = json.loads(file.read_text(encoding="utf-8"))["chat"]["messages"][
                "status"
            ]
            assert "{{count}}" in status["usedSteps"], file.name
            assert "{{count}}" in status["usedTools"], file.name
