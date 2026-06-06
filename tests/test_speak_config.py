"""``LocalPlayback`` / ``SpeakConfig`` parsing + ``[teatree.speak]`` resolution (#2060).

The v3 schema: a ``[teatree.speak]`` sub-table with a ``local`` enum
(``off`` / ``dm`` / ``all``) and a ``slack`` bool. The two axes are fully
independent. Covers: defaults when absent, new-table parse, partial keys,
a clean ``ValueError`` on a typo, and the per-overlay sub-table merge.
"""

from pathlib import Path

import pytest

from teatree.config import get_effective_settings, load_config
from teatree.config_speak import resolve_speak
from teatree.types import LocalPlayback, SpeakConfig


class TestLocalPlaybackEnum:
    def test_parse_each_value(self) -> None:
        assert LocalPlayback.parse("off") is LocalPlayback.OFF
        assert LocalPlayback.parse("dm") is LocalPlayback.DM
        assert LocalPlayback.parse("all") is LocalPlayback.ALL

    def test_parse_is_case_and_whitespace_insensitive(self) -> None:
        assert LocalPlayback.parse("  ALL ") is LocalPlayback.ALL

    def test_parse_rejects_typo(self) -> None:
        with pytest.raises(ValueError, match="Invalid speak local"):
            LocalPlayback.parse("everything")

    def test_default_is_off(self) -> None:
        assert SpeakConfig().local is LocalPlayback.OFF


class TestSpeakConfigHelpers:
    def test_disabled_by_default(self) -> None:
        cfg = SpeakConfig()
        assert cfg.enabled() is False
        assert cfg.speaks_dms() is False
        assert cfg.speaks_in_client_turns() is False

    def test_enabled_when_local_on_or_slack_on(self) -> None:
        assert SpeakConfig(local=LocalPlayback.DM).enabled() is True
        assert SpeakConfig(local=LocalPlayback.ALL).enabled() is True
        assert SpeakConfig(slack=True).enabled() is True
        assert SpeakConfig(local=LocalPlayback.OFF, slack=False).enabled() is False

    def test_speaks_dms_when_local_dm_or_all(self) -> None:
        assert SpeakConfig(local=LocalPlayback.DM).speaks_dms() is True
        assert SpeakConfig(local=LocalPlayback.ALL).speaks_dms() is True
        assert SpeakConfig(local=LocalPlayback.OFF).speaks_dms() is False
        # slack never enables local play
        assert SpeakConfig(local=LocalPlayback.OFF, slack=True).speaks_dms() is False

    def test_speaks_in_client_turns_only_when_local_all_regardless_of_slack(self) -> None:
        assert SpeakConfig(local=LocalPlayback.ALL).speaks_in_client_turns() is True
        assert SpeakConfig(local=LocalPlayback.ALL, slack=True).speaks_in_client_turns() is True
        assert SpeakConfig(local=LocalPlayback.DM).speaks_in_client_turns() is False
        assert SpeakConfig(local=LocalPlayback.OFF).speaks_in_client_turns() is False

    def test_to_dict_has_local_string_and_slack_bool(self) -> None:
        assert SpeakConfig(local=LocalPlayback.ALL, slack=True).to_dict() == {"local": "all", "slack": True}
        assert SpeakConfig().to_dict() == {"local": "off", "slack": False}


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
        assert load_config().user.speak == SpeakConfig(local=LocalPlayback.OFF, slack=False)

    def test_default_when_file_missing(self, config_file: Path) -> None:
        assert load_config().user.speak == SpeakConfig()

    def test_new_table_parsed(self, config_file: Path) -> None:
        _write(config_file, '[teatree.speak]\nlocal = "all"\nslack = true\n')
        assert load_config().user.speak == SpeakConfig(local=LocalPlayback.ALL, slack=True)

    def test_new_table_partial_keys_default_the_rest(self, config_file: Path) -> None:
        _write(config_file, "[teatree.speak]\nslack = true\n")
        assert load_config().user.speak == SpeakConfig(local=LocalPlayback.OFF, slack=True)

    def test_unknown_v2_keys_are_silently_inert(self, config_file: Path) -> None:
        # Clean cutover: the removed v2 keys carry no meaning in v3 and are silently ignored.
        _write(config_file, '[teatree.speak]\nlocal = "dm"\nslack_audio = true\nscope = "all"\n')
        assert load_config().user.speak == SpeakConfig(local=LocalPlayback.DM, slack=False)

    def test_v2_local_boolean_value_fails_clean_not_attributeerror(self, config_file: Path) -> None:
        # `local` is reused with a new string type — a leftover v2 `local = true`
        # is a misconfiguration that must fail loudly, not crash on `.strip()`.
        _write(config_file, "[teatree.speak]\nlocal = true\n")
        with pytest.raises(ValueError, match="Invalid speak local"):
            load_config()

    def test_invalid_local_raises_clean_valueerror(self, config_file: Path) -> None:
        _write(config_file, '[teatree.speak]\nlocal = "everywhere"\n')
        with pytest.raises(ValueError, match="Invalid speak local"):
            load_config()


class TestResolveSpeakDirect:
    def test_off_when_empty(self) -> None:
        assert resolve_speak({}) == SpeakConfig()

    def test_unknown_top_level_keys_ignored(self) -> None:
        assert resolve_speak({"workspace_dir": "~/workspace"}) == SpeakConfig()

    def test_sub_table_function_level(self) -> None:
        assert resolve_speak({"speak": {"local": "all", "slack": True}}) == SpeakConfig(
            local=LocalPlayback.ALL, slack=True
        )


class TestPerOverlayOverride:
    def test_per_overlay_speak_sub_table_merges_onto_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text(
            (
                "[teatree.speak]\n"
                'local = "all"\n'
                "[overlays.my-overlay]\n"
                'class = "x.y:Z"\n'
                "[overlays.my-overlay.speak]\n"
                "slack = true\n"
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.CONFIG_PATH", config_path)
        effective = get_effective_settings("my-overlay")
        assert effective.speak == SpeakConfig(local=LocalPlayback.ALL, slack=True)

    def test_per_overlay_local_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_path = tmp_path / ".teatree.toml"
        config_path.write_text(
            (
                "[teatree.speak]\n"
                "slack = true\n"
                "[overlays.my-overlay]\n"
                'class = "x.y:Z"\n'
                "[overlays.my-overlay.speak]\n"
                'local = "all"\n'
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.CONFIG_PATH", config_path)
        effective = get_effective_settings("my-overlay")
        assert effective.speak == SpeakConfig(local=LocalPlayback.ALL, slack=True)
