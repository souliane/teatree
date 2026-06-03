"""``SpeakMode`` / ``SpeakTarget`` enum parsing + TOML config resolution (#1791).

Pure-logic coverage of the two enums (``.parse`` mirroring the sibling
``SlackVoiceClassifierMode`` / ``OnBehalfPostMode``) and the loader's
``speak_mode`` / ``speak_target`` resolution: default ``off`` / ``local``
when absent, flat-key parse, a clean ``ValueError`` on a typo so a silent
mode mis-resolution never lands, and per-overlay override registration.
"""

from pathlib import Path

import pytest

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, discover_overlays, load_config
from teatree.types import SpeakMode, SpeakTarget


class TestSpeakModeEnum:
    def test_parse_each_value(self) -> None:
        assert SpeakMode.parse("off") is SpeakMode.OFF
        assert SpeakMode.parse("im-only") is SpeakMode.IM_ONLY
        assert SpeakMode.parse("all") is SpeakMode.ALL

    def test_parse_is_case_and_whitespace_insensitive(self) -> None:
        assert SpeakMode.parse("  IM-ONLY ") is SpeakMode.IM_ONLY

    def test_parse_rejects_typo(self) -> None:
        with pytest.raises(ValueError, match="Invalid speak_mode"):
            SpeakMode.parse("loud")

    def test_default_is_off(self) -> None:
        assert SpeakMode.OFF == "off"


class TestSpeakTargetEnum:
    def test_parse_each_value(self) -> None:
        assert SpeakTarget.parse("local") is SpeakTarget.LOCAL
        assert SpeakTarget.parse("slack-audio") is SpeakTarget.SLACK_AUDIO
        assert SpeakTarget.parse("both") is SpeakTarget.BOTH

    def test_parse_rejects_typo(self) -> None:
        with pytest.raises(ValueError, match="Invalid speak_target"):
            SpeakTarget.parse("phone")

    def test_includes_local(self) -> None:
        assert SpeakTarget.LOCAL.includes_local()
        assert SpeakTarget.BOTH.includes_local()
        assert not SpeakTarget.SLACK_AUDIO.includes_local()

    def test_includes_slack(self) -> None:
        assert SpeakTarget.SLACK_AUDIO.includes_slack()
        assert SpeakTarget.BOTH.includes_slack()
        assert not SpeakTarget.LOCAL.includes_slack()


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


def _write(cfg: Path, body: str) -> None:
    cfg.write_text(body, encoding="utf-8")


class TestSpeakModeResolution:
    def test_default_is_off_when_absent(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        assert load_config().user.speak_mode is SpeakMode.OFF

    def test_default_is_off_when_file_missing(self, config_file: Path) -> None:
        assert load_config().user.speak_mode is SpeakMode.OFF

    def test_flat_im_only(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_mode = "im-only"\n')
        assert load_config().user.speak_mode is SpeakMode.IM_ONLY

    def test_flat_all(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_mode = "all"\n')
        assert load_config().user.speak_mode is SpeakMode.ALL

    def test_invalid_value_raises(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_mode = "loud"\n')
        with pytest.raises(ValueError, match="Invalid speak_mode"):
            load_config()


class TestSpeakTargetResolution:
    def test_default_is_local_when_absent(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        assert load_config().user.speak_target is SpeakTarget.LOCAL

    def test_flat_slack_audio(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_target = "slack-audio"\n')
        assert load_config().user.speak_target is SpeakTarget.SLACK_AUDIO

    def test_flat_both(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_target = "both"\n')
        assert load_config().user.speak_target is SpeakTarget.BOTH

    def test_invalid_value_raises(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_target = "phone"\n')
        with pytest.raises(ValueError, match="Invalid speak_target"):
            load_config()


class TestPerOverlayOverride:
    def test_both_settings_are_overlay_overridable(self) -> None:
        assert OVERLAY_OVERRIDABLE_SETTINGS["speak_mode"]("all") is SpeakMode.ALL
        assert OVERLAY_OVERRIDABLE_SETTINGS["speak_target"]("both") is SpeakTarget.BOTH

    def test_overlay_table_parses_both_overrides(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text(
            (
                "[teatree]\n"
                'speak_mode = "im-only"\n'
                'speak_target = "local"\n'
                "[overlays.my-overlay]\n"
                'class = "x.y:Z"\n'
                'speak_mode = "all"\n'
                'speak_target = "both"\n'
            ),
            encoding="utf-8",
        )
        by_name = {e.name: e for e in discover_overlays(config_path=config_path)}
        assert by_name["my-overlay"].overrides["speak_mode"] is SpeakMode.ALL
        assert by_name["my-overlay"].overrides["speak_target"] is SpeakTarget.BOTH
