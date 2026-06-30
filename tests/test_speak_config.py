"""``LocalPlayback`` / ``SpeakConfig`` parsing + DB-home ``speak`` resolution (#2060).

The schema: a ``speak`` config with a ``local`` enum (``off`` / ``dm`` / ``all``)
and a ``slack`` bool, fully independent. eliminate-~/.teatree.toml made ``speak``
DB-home — it resolves from a JSON-dict ``ConfigSetting`` row (rebuilt bespoke via
``speak_from_subtable``). Covers: defaults when absent, parse, partial keys, a clean
``ValueError`` on a typo, and the per-overlay row merge.
"""

from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import get_effective_settings
from teatree.config_speak import resolve_speak
from teatree.core.models import ConfigSetting
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


class TestSpeakDbResolution(TestCase):
    """eliminate-~/.teatree.toml: ``speak`` resolves from a JSON-dict ``ConfigSetting`` row.

    The stored dict is rebuilt bespoke by the resolver via ``speak_from_subtable`` —
    the same builder the old ``[teatree.speak]`` reader used, so partial keys, unknown
    keys, and the loud ``ValueError`` on a bad ``local`` are unchanged.
    """

    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_facade, "CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_default_when_no_row(self) -> None:
        assert get_effective_settings().speak == SpeakConfig(local=LocalPlayback.OFF, slack=False)

    def test_row_parsed(self) -> None:
        ConfigSetting.objects.set_value("speak", value={"local": "all", "slack": True})
        assert get_effective_settings().speak == SpeakConfig(local=LocalPlayback.ALL, slack=True)

    def test_partial_keys_default_the_rest(self) -> None:
        ConfigSetting.objects.set_value("speak", value={"slack": True})
        assert get_effective_settings().speak == SpeakConfig(local=LocalPlayback.OFF, slack=True)

    def test_unknown_keys_are_silently_inert(self) -> None:
        ConfigSetting.objects.set_value("speak", value={"local": "dm", "slack_audio": True, "scope": "all"})
        assert get_effective_settings().speak == SpeakConfig(local=LocalPlayback.DM, slack=False)

    def test_local_boolean_value_raises_clean_valueerror(self) -> None:
        # A corrupt ``local = true`` (bool) must fail loudly on read, not crash on .strip().
        ConfigSetting.objects.set_value("speak", value={"local": True})
        with pytest.raises(ValueError, match="Invalid speak local"):
            get_effective_settings()

    def test_invalid_local_raises_clean_valueerror(self) -> None:
        ConfigSetting.objects.set_value("speak", value={"local": "everywhere"})
        with pytest.raises(ValueError, match="Invalid speak local"):
            get_effective_settings()


class TestResolveSpeakDirect:
    def test_off_when_empty(self) -> None:
        assert resolve_speak({}) == SpeakConfig()

    def test_unknown_top_level_keys_ignored(self) -> None:
        assert resolve_speak({"workspace_dir": "~/workspace"}) == SpeakConfig()

    def test_sub_table_function_level(self) -> None:
        assert resolve_speak({"speak": {"local": "all", "slack": True}}) == SpeakConfig(
            local=LocalPlayback.ALL, slack=True
        )


class TestPerOverlaySpeakMerge(TestCase):
    """eliminate-~/.teatree.toml: a per-overlay ``speak`` DB row MERGES onto the global base.

    The one non-generic structured override (``resolution._resolve_speak_db``): the
    global ``speak`` row sets the base, the overlay-scope row overrides only the keys
    it carries — the DB equivalent of the old ``[overlays.<name>.speak]`` sub-table merge.
    """

    @pytest.fixture(autouse=True)
    def _sandbox(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(config_facade, "CONFIG_PATH", tmp_path / ".teatree.toml")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_overlay_row_inherits_global_local(self) -> None:
        # Global sets local=all; the overlay row sets only slack → local is inherited.
        ConfigSetting.objects.set_value("speak", value={"local": "all"})
        ConfigSetting.objects.set_value("speak", value={"slack": True}, scope="my-overlay")
        assert get_effective_settings("my-overlay").speak == SpeakConfig(local=LocalPlayback.ALL, slack=True)

    def test_overlay_row_overrides_local(self) -> None:
        # Global sets slack=true; the overlay row sets local=all → slack inherited, local overridden.
        ConfigSetting.objects.set_value("speak", value={"slack": True})
        ConfigSetting.objects.set_value("speak", value={"local": "all"}, scope="my-overlay")
        assert get_effective_settings("my-overlay").speak == SpeakConfig(local=LocalPlayback.ALL, slack=True)
