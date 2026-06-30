# test-path: cross-cutting
"""The hard partition in the live resolver: a setting reads from ONE home (#1775).

A DB-home field resolves from ``ConfigSetting`` (global + overlay rows) + env
ONLY — a ``[teatree]`` / ``[overlays.<name>]`` value for it is ignored on read.
A TOML-home field resolves from ``[teatree]`` / ``[overlays.<name>]`` + env ONLY
— a ``ConfigSetting`` row for it is ignored on read. The additive "DB row
overrides same-key TOML value" behaviour is intentionally removed.

Integration-first: real TOML fixtures under ``tmp_path`` with
``teatree.config.CONFIG_PATH`` monkeypatched, against the real DB.
"""

import logging
from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import get_effective_settings
from teatree.config.enums import Mode, OnBehalfPostMode
from teatree.core.models import ConfigSetting
from teatree.types import LocalPlayback

from ._shared import _write_toml


class TestDbHomeIgnoresToml(TestCase):
    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)
        self.monkeypatch = monkeypatch

    def test_db_home_field_falls_to_default_with_empty_table(self) -> None:
        # The DB is the sole source for a DB-home field: an empty table resolves
        # the dataclass default, NOT any [teatree] value (there is none).
        _write_toml(self.config_path, "[teatree]\n")
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().issue_implementer_enabled is False

    def test_db_home_field_resolves_from_db_row(self) -> None:
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        assert get_effective_settings().issue_implementer_enabled is True

    def test_db_home_field_ignores_a_teatree_toml_value(self) -> None:
        # A DB-home key set in [teatree] is NOT read — it is ignored on read (its
        # home is the DB; migrate it with `t3 <overlay> config_setting import`).
        # The resolution invariant here: with no DB row, the resolved value is the
        # dataclass default, not the TOML value. We assert via a DB row that the
        # DB is the sole authority: the DB row value wins and there is no TOML
        # layer beneath it for this key.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", value=7)
        assert get_effective_settings().issue_implementer_max_concurrent == 7
        ConfigSetting.objects.clear("issue_implementer_max_concurrent")
        # Cleared -> dataclass default (1), proving there is no [teatree] tier.
        assert get_effective_settings().issue_implementer_max_concurrent == 1

    def test_newly_db_home_field_resolves_from_db_row(self) -> None:
        # repo_mode was file-only today; it is now DB-home and resolves from a row.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("repo_mode", "solo")
        assert get_effective_settings().repo_mode == "solo"

    def test_autoload_db_home_resolves_from_db_row(self) -> None:
        # eliminate-~/.teatree.toml: the #256 engagement flag ``autoload`` is DB-home
        # now — a global ``ConfigSetting`` row resolves it (the DB is its sole home).
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("autoload", value=True)
        assert get_effective_settings().autoload is True

    def test_autoload_db_home_ignores_a_teatree_toml_value(self) -> None:
        # A ``[teatree] autoload`` value is ignored on read (its home is the DB):
        # with no DB row the resolved value is the dataclass default (False), never
        # the TOML ``true``.
        _write_toml(self.config_path, "[teatree]\nautoload = true\n")
        assert get_effective_settings().autoload is False


class TestSpeakDbHomeIgnoresToml(TestCase):
    """eliminate-~/.teatree.toml: ``speak`` is DB-home — the last carve-out field moved.

    It resolves from the ``ConfigSetting`` store (rebuilt bespoke from the stored JSON
    dict), and a ``[teatree.speak]`` sub-table is ignored on read.
    """

    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        self.monkeypatch = monkeypatch

    def test_speak_resolves_from_db_row(self) -> None:
        # A global ``speak`` ConfigSetting row (a JSON dict) resolves to the dataclass.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("speak", value={"local": "dm"})
        assert get_effective_settings().speak.local is LocalPlayback.DM

    def test_speak_ignores_a_teatree_speak_table(self) -> None:
        # A ``[teatree.speak]`` value is ignored on read (speak's home is the DB) —
        # with no DB row the resolved value is the dataclass default (OFF).
        _write_toml(self.config_path, '[teatree]\n\n[teatree.speak]\nlocal = "dm"\n')
        assert get_effective_settings().speak.local is LocalPlayback.OFF

    def test_statusline_chain_resolves_from_db_not_teatree(self) -> None:
        # eliminate-~/.teatree.toml: statusline_chain is DB-home — the bash
        # statusline hook reads it from the canonical sqlite via json_each, so the
        # ConfigSetting row wins and a [teatree] value is ignored on read.
        _write_toml(self.config_path, '[teatree]\nstatusline_chain = ["custom/*.sh"]\n')
        ConfigSetting.objects.set_value("statusline_chain", value=["db/*.sh"])
        assert get_effective_settings().statusline_chain == ["db/*.sh"]


class TestOverlayScopeLayering(TestCase):
    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)
        _write_toml(self.config_path, '[teatree]\n\n[overlays.my-overlay]\nclass = "x.y:Z"\n')
        self.monkeypatch = monkeypatch

    def test_overlay_scoped_db_row_beats_global_db_row_for_db_home(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().issue_implementer_enabled is True

    # (eliminate-~/.teatree.toml removed the generic per-overlay-TOML-scalar override:
    # ``TOML_OVERLAY_OVERRIDABLE_SETTINGS`` is now empty — a setting's per-overlay
    # override lives in a ``ConfigSetting`` overlay-scope row, covered by
    # ``test_overlay_scoped_db_row_beats_global_db_row_for_db_home`` above. The former
    # ``test_overlay_scoped_toml_value_beats_global_toml_for_toml_home`` is obsolete.)

    def test_overlay_db_row_for_speak_merges_onto_global(self) -> None:
        # eliminate-~/.teatree.toml: ``speak`` is DB-home and its per-overlay row
        # MERGES onto the global ``speak`` base — only the keys the overlay row sets
        # override. Here the global row sets local=all+slack on; the overlay row sets
        # only slack off, so local stays ``all`` and slack flips off.
        _write_toml(self.config_path, '[teatree]\n\n[overlays.my-overlay]\nclass = "x.y:Z"\n')
        ConfigSetting.objects.set_value("speak", value={"local": "all", "slack": True})
        ConfigSetting.objects.set_value("speak", value={"slack": False}, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        speak = get_effective_settings().speak
        assert speak.local is LocalPlayback.ALL
        assert speak.slack is False


class TestDbHomeKeyInOverlayTomlIsLoud(TestCase):
    """The resolver WARNs (never silently drops) a DB-home key in a TOML overlay layer.

    The footgun the warning closes: a user writes ``[overlays.foo] mode = "auto"``
    in ``~/.teatree.toml`` expecting auto mode, but ``mode`` is DB-home — the
    ``_toml_home`` filter in the resolver drops the key. With NO DB row beneath it
    the dropped value also has no effect, so the setting silently resolves to its
    default and nothing tells the operator their TOML was ignored. The resolver
    must surface the drop loud so the operator can migrate the key with
    ``config_setting set`` / ``config_setting import``.
    """

    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_MODE", raising=False)
        self.monkeypatch = monkeypatch

    def test_db_home_key_in_overlay_toml_warns_even_without_db_row(self) -> None:
        # No DB row beneath the dropped key — the value silently has no effect
        # today. The resolver must WARN naming the key and the overlay scope.
        _write_toml(
            self.config_path,
            '[teatree]\n\n[overlays.my-overlay]\nclass = "x.y:Z"\nmode = "auto"\n',
        )
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        with self.assertLogs("teatree.config", level=logging.WARNING) as captured:
            settings = get_effective_settings()
        # The key was dropped (its home is the DB) — mode is still the default.
        assert settings.mode is Mode.INTERACTIVE
        joined = "\n".join(captured.output)
        assert "mode" in joined
        assert "my-overlay" in joined
        assert "DB-home" in joined

    def test_no_warning_when_overlay_toml_has_no_user_settings_keys(self) -> None:
        # A clean overlay table with only structural keys (no ``UserSettings`` field)
        # emits no DB-home drop warning. eliminate-~/.teatree.toml emptied the TOML
        # carve-out, so ANY UserSettings key here is DB-home and would warn; a table
        # that carries none (just the overlay ``class`` registration) is silent.
        _write_toml(
            self.config_path,
            '[teatree]\n\n[overlays.my-overlay]\nclass = "x.y:Z"\n',
        )
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        logger = logging.getLogger("teatree.config")
        with self.assertNoLogs(logger, level=logging.WARNING):
            get_effective_settings()


class TestEnvWinsForBothHomes(TestCase):
    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)
        monkeypatch.delenv("T3_MODE", raising=False)
        self.monkeypatch = monkeypatch

    def test_env_wins_over_db_home_db_row(self) -> None:
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        self.monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "true")
        assert get_effective_settings().issue_implementer_enabled is True

    def test_env_wins_over_toml_home_table_value(self) -> None:
        _write_toml(self.config_path, '[teatree]\nmode = "interactive"\n')
        self.monkeypatch.setenv("T3_MODE", "auto")
        assert get_effective_settings().mode is Mode.AUTO


class TestAutonomyCollapseWithDbHomeGates(TestCase):
    """The three approval gates are now DB-home.

    The autonomy collapse must still honour an explicit global pin, now detected
    from the GLOBAL-scope DB rows rather than the ``[teatree]`` TOML table.
    """

    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_MODE", raising=False)
        self.monkeypatch = monkeypatch

    def test_full_autonomy_collapses_unpinned_db_home_gates(self) -> None:
        # autonomy is DB-home now: set it via a ConfigSetting row, not [teatree].
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("autonomy", "full")
        settings = get_effective_settings()
        assert settings.require_human_approval_to_merge is False
        assert settings.require_human_approval_to_answer is False
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.mode is Mode.AUTO

    def test_autonomy_collapse_respects_db_global_pin(self) -> None:
        # A user who pins require_human_approval_to_merge=True via a GLOBAL DB row
        # keeps that gate even under full autonomy — the pin is detected from the
        # resolved global-scope DB rows, not the [teatree] TOML table.
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("autonomy", "full")
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=True)
        settings = get_effective_settings()
        assert settings.require_human_approval_to_merge is True
        # The unpinned gates still collapse.
        assert settings.require_human_approval_to_answer is False


class TestSpeakAndMrReminderDbHome(TestCase):
    """eliminate-~/.teatree.toml: the last two carve-out fields resolve from the DB.

    ``speak`` keeps its per-overlay MERGE semantics (the global row is the base, the
    overlay row overrides only the keys it carries); ``mr_reminder`` resolves from a
    global ``ConfigSetting`` JSON-dict row.
    """

    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        self.monkeypatch = monkeypatch

    def test_speak_overlay_row_merges_onto_base(self) -> None:
        _write_toml(self.config_path, '[teatree]\n\n[overlays.my-overlay]\nclass = "x.y:Z"\n')
        ConfigSetting.objects.set_value("speak", value={"local": "dm", "slack": False})
        ConfigSetting.objects.set_value("speak", value={"slack": True}, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        speak = get_effective_settings().speak
        # local inherited from the global base, slack overridden by the overlay row.
        assert speak.local is LocalPlayback.DM
        assert speak.slack is True

    def test_mr_reminder_resolves_from_db_row(self) -> None:
        _write_toml(self.config_path, "[teatree]\n")
        ConfigSetting.objects.set_value("mr_reminder", value={"channels": {"acme/widget": "#widget-mrs"}})
        mr_reminder = get_effective_settings().mr_reminder
        assert mr_reminder.channels == (("acme/widget", "#widget-mrs"),)
