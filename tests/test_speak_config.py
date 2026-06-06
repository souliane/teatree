"""``SpeakScope`` / ``SpeakConfig`` parsing + ``[teatree.speak]`` resolution (#2050).

The new schema: a ``[teatree.speak]`` sub-table with two booleans
(``local`` / ``slack_audio``) and a ``scope`` enum (``dm`` / ``all``).
Legacy flat ``speak_mode`` / ``speak_target`` keys auto-map to the new
shape for one transition release (mirrors the ``on_behalf_post_mode``
legacy alias). Covers: defaults when absent, new-table parse, the
legacy map (incl. the live ``im-only`` / ``both`` config), new-table
precedence over legacy, a clean ``ValueError`` on a typo, and the
per-overlay sub-table merge.
"""

from pathlib import Path

import pytest

from teatree.config import get_effective_settings, load_config
from teatree.config_speak import resolve_speak
from teatree.types import SpeakConfig, SpeakScope


class TestSpeakScopeEnum:
    def test_parse_each_value(self) -> None:
        assert SpeakScope.parse("dm") is SpeakScope.DM
        assert SpeakScope.parse("all") is SpeakScope.ALL

    def test_parse_is_case_and_whitespace_insensitive(self) -> None:
        assert SpeakScope.parse("  ALL ") is SpeakScope.ALL

    def test_parse_rejects_typo(self) -> None:
        with pytest.raises(ValueError, match="Invalid speak scope"):
            SpeakScope.parse("everything")

    def test_default_is_dm(self) -> None:
        assert SpeakConfig().scope is SpeakScope.DM


class TestSpeakConfigHelpers:
    def test_disabled_by_default(self) -> None:
        cfg = SpeakConfig()
        assert cfg.enabled() is False
        assert cfg.speaks_dms() is False

    def test_enabled_when_any_destination_on(self) -> None:
        assert SpeakConfig(local=True).enabled() is True
        assert SpeakConfig(slack_audio=True).enabled() is True

    def test_speaks_in_client_turns_only_when_all_local_and_not_slack_audio(self) -> None:
        assert SpeakConfig(local=True, scope=SpeakScope.ALL).speaks_in_client_turns() is True
        assert SpeakConfig(local=True, slack_audio=True, scope=SpeakScope.ALL).speaks_in_client_turns() is False
        assert SpeakConfig(local=True, scope=SpeakScope.DM).speaks_in_client_turns() is False
        assert SpeakConfig(local=False, scope=SpeakScope.ALL).speaks_in_client_turns() is False


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


def _write(cfg: Path, body: str) -> None:
    cfg.write_text(body, encoding="utf-8")


class TestNewTableResolution:
    def test_default_when_absent(self, config_file: Path) -> None:
        _write(config_file, "[teatree]\n")
        assert load_config().user.speak == SpeakConfig(local=False, slack_audio=False, scope=SpeakScope.DM)

    def test_default_when_file_missing(self, config_file: Path) -> None:
        assert load_config().user.speak == SpeakConfig()

    def test_new_table_parsed(self, config_file: Path) -> None:
        _write(config_file, '[teatree.speak]\nlocal = true\nslack_audio = true\nscope = "all"\n')
        assert load_config().user.speak == SpeakConfig(local=True, slack_audio=True, scope=SpeakScope.ALL)

    def test_new_table_partial_keys_default_the_rest(self, config_file: Path) -> None:
        _write(config_file, "[teatree.speak]\nslack_audio = true\n")
        assert load_config().user.speak == SpeakConfig(local=False, slack_audio=True, scope=SpeakScope.DM)

    def test_invalid_scope_raises_clean_valueerror(self, config_file: Path) -> None:
        _write(config_file, '[teatree.speak]\nscope = "everywhere"\n')
        with pytest.raises(ValueError, match="Invalid speak scope"):
            load_config()


class TestLegacyMigration:
    def test_legacy_im_only_both_maps(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_mode = "im-only"\nspeak_target = "both"\n')
        assert load_config().user.speak == SpeakConfig(local=True, slack_audio=True, scope=SpeakScope.DM)

    def test_legacy_all_slack_audio_maps(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_mode = "all"\nspeak_target = "slack-audio"\n')
        assert load_config().user.speak == SpeakConfig(local=False, slack_audio=True, scope=SpeakScope.ALL)

    def test_legacy_local_only_maps(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_mode = "im-only"\nspeak_target = "local"\n')
        assert load_config().user.speak == SpeakConfig(local=True, slack_audio=False, scope=SpeakScope.DM)

    def test_legacy_off_maps_both_destinations_false(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_mode = "off"\nspeak_target = "both"\n')
        assert load_config().user.speak == SpeakConfig(local=False, slack_audio=False, scope=SpeakScope.DM)

    def test_legacy_target_only_defaults_scope_dm(self, config_file: Path) -> None:
        _write(config_file, '[teatree]\nspeak_target = "both"\n')
        assert load_config().user.speak == SpeakConfig(local=True, slack_audio=True, scope=SpeakScope.DM)

    def test_new_table_wins_over_legacy(self, config_file: Path) -> None:
        _write(
            config_file,
            '[teatree]\nspeak_mode = "all"\nspeak_target = "both"\n[teatree.speak]\nlocal = true\n',
        )
        assert load_config().user.speak == SpeakConfig(local=True, slack_audio=False, scope=SpeakScope.DM)


class TestResolveSpeakDirect:
    def test_off_when_empty(self) -> None:
        assert resolve_speak({}) == SpeakConfig()

    def test_legacy_mapping_function_level(self) -> None:
        assert resolve_speak({"speak_mode": "im-only", "speak_target": "both"}) == SpeakConfig(
            local=True, slack_audio=True, scope=SpeakScope.DM
        )


class TestPerOverlayOverride:
    def test_per_overlay_speak_sub_table_merges_onto_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text(
            (
                "[teatree.speak]\n"
                "local = true\n"
                'scope = "all"\n'
                "[overlays.my-overlay]\n"
                'class = "x.y:Z"\n'
                "[overlays.my-overlay.speak]\n"
                "slack_audio = true\n"
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.CONFIG_PATH", config_path)
        effective = get_effective_settings("my-overlay")
        assert effective.speak == SpeakConfig(local=True, slack_audio=True, scope=SpeakScope.ALL)

    def test_per_overlay_scope_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text(
            (
                "[teatree.speak]\n"
                "local = true\n"
                "[overlays.my-overlay]\n"
                'class = "x.y:Z"\n'
                "[overlays.my-overlay.speak]\n"
                'scope = "all"\n'
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.CONFIG_PATH", config_path)
        effective = get_effective_settings("my-overlay")
        assert effective.speak == SpeakConfig(local=True, slack_audio=False, scope=SpeakScope.ALL)
