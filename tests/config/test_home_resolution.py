# test-path: cross-cutting
"""The hard partition in the live resolver: a DB-home setting reads from the DB (#1775).

Every ``UserSettings`` field is DB-home: it resolves from ``ConfigSetting`` (global +
overlay rows) + the ``T3_*`` env layer, and an empty table resolves the dataclass
default. A DB-home key placed inside an ``overlays`` registry entry is dropped on
read (LOUD, never silent) — its sole home is a scoped ``ConfigSetting`` row.

Integration-first: real ``ConfigSetting`` rows against the real DB; the overlays
registry seeded into the cold-path sqlite (``config_db``).
"""

import logging
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.config.enums import Mode, OnBehalfPostMode
from teatree.core.models import ConfigSetting
from teatree.types import LocalPlayback

from ._shared import _seed_config_db


def _drop_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "DB-home settings" in r.getMessage()]


class TestDbHomeResolution(TestCase):
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)

    def test_db_home_field_falls_to_default_with_empty_table(self) -> None:
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().issue_implementer_enabled is False

    def test_db_home_field_resolves_from_db_row(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        assert get_effective_settings().issue_implementer_enabled is True

    def test_db_is_the_sole_authority_for_a_db_home_field(self) -> None:
        # A DB row is the sole source; clearing it restores the dataclass default
        # (there is no tier beneath the DB for a DB-home key).
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", value=7)
        assert get_effective_settings().issue_implementer_max_concurrent == 7
        ConfigSetting.objects.clear("issue_implementer_max_concurrent")
        assert get_effective_settings().issue_implementer_max_concurrent == 3

    def test_newly_db_home_field_resolves_from_db_row(self) -> None:
        ConfigSetting.objects.set_value("repo_mode", "solo")
        assert get_effective_settings().repo_mode == "solo"

    def test_autoload_resolves_from_db_row(self) -> None:
        ConfigSetting.objects.set_value("autoload", value=True)
        assert get_effective_settings().autoload is True


class TestSpeakDbHome(TestCase):
    """``speak`` is DB-home — resolved from a JSON-dict ``ConfigSetting`` row."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_speak_resolves_from_db_row(self) -> None:
        ConfigSetting.objects.set_value("speak", value={"local": "dm"})
        assert get_effective_settings().speak.local is LocalPlayback.DM

    def test_statusline_chain_resolves_from_db_row(self) -> None:
        ConfigSetting.objects.set_value("statusline_chain", value=["db/*.sh"])
        assert get_effective_settings().statusline_chain == ["db/*.sh"]


class TestOverlayScopeLayering(TestCase):
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)
        self.monkeypatch = monkeypatch

    def test_overlay_scoped_db_row_beats_global_db_row_for_db_home(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().issue_implementer_enabled is True

    def test_overlay_db_row_for_speak_merges_onto_global(self) -> None:
        # The per-overlay ``speak`` row MERGES onto the global base — only the keys
        # the overlay row sets override. Here the global row sets local=all + slack on;
        # the overlay row sets only slack off, so local stays ``all`` and slack flips off.
        ConfigSetting.objects.set_value("speak", value={"local": "all", "slack": True})
        ConfigSetting.objects.set_value("speak", value={"slack": False}, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        speak = get_effective_settings().speak
        assert speak.local is LocalPlayback.ALL
        assert speak.slack is False


class TestDbHomeKeyInOverlayRegistryIsLoud:
    """The resolver WARNs (never silently drops) a DB-home key in an ``overlays`` registry entry.

    A DB-home key placed inside the registry entry (rather than a scoped
    ``ConfigSetting`` row) is dropped on read; with nothing beneath it the value has
    no effect, so the resolver surfaces the drop loud so the operator can migrate it.
    """

    @pytest.mark.usefixtures("no_installed_overlays")
    def test_db_home_key_in_overlay_registry_warns(
        self, config_db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        _seed_config_db(config_db, overlays={"my-overlay": {"class": "x.y:Z", "mode": "auto"}})
        with caplog.at_level(logging.WARNING, logger="teatree.config"):
            settings = get_effective_settings()
        assert settings.mode is Mode.INTERACTIVE
        joined = "\n".join(_drop_warnings(caplog))
        assert "mode" in joined
        assert "my-overlay" in joined

    @pytest.mark.usefixtures("no_installed_overlays")
    def test_no_warning_when_overlay_registry_has_no_user_settings_keys(
        self, config_db: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        _seed_config_db(config_db, overlays={"my-overlay": {"class": "x.y:Z"}})
        with caplog.at_level(logging.WARNING, logger="teatree.config"):
            get_effective_settings()
        assert _drop_warnings(caplog) == []


class TestEnvWinsOverDbHome(TestCase):
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ISSUE_IMPLEMENTER_ENABLED", raising=False)
        monkeypatch.delenv("T3_MODE", raising=False)
        self.monkeypatch = monkeypatch

    def test_env_wins_over_db_home_db_row(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        self.monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "true")
        assert get_effective_settings().issue_implementer_enabled is True

    def test_env_wins_over_db_home_default(self) -> None:
        self.monkeypatch.setenv("T3_MODE", "auto")
        assert get_effective_settings().mode is Mode.AUTO


class TestAutonomyCollapseWithDbHomeGates(TestCase):
    """The three approval gates are DB-home; the autonomy collapse honours a global DB pin."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_MODE", raising=False)

    def test_full_autonomy_collapses_unpinned_db_home_gates(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full")
        settings = get_effective_settings()
        assert settings.require_human_approval_to_answer is False
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
        assert settings.mode is Mode.AUTO
        # #3630: the merge review gate is not tier-governed and keeps its default.
        assert settings.require_human_approval_to_merge is True

    def test_autonomy_collapse_respects_db_global_pin(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "full")
        ConfigSetting.objects.set_value("require_human_approval_to_answer", value=True)
        settings = get_effective_settings()
        assert settings.require_human_approval_to_answer is True
        assert settings.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE


class TestSpeakAndMrReminderDbHome(TestCase):
    """``speak`` keeps its per-overlay MERGE semantics; ``mr_reminder`` resolves from a global row."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        self.monkeypatch = monkeypatch

    def test_speak_overlay_row_merges_onto_base(self) -> None:
        ConfigSetting.objects.set_value("speak", value={"local": "dm", "slack": False})
        ConfigSetting.objects.set_value("speak", value={"slack": True}, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        speak = get_effective_settings().speak
        assert speak.local is LocalPlayback.DM
        assert speak.slack is True

    def test_mr_reminder_resolves_from_db_row(self) -> None:
        ConfigSetting.objects.set_value("mr_reminder", value={"channels": {"acme/widget": "#widget-mrs"}})
        mr_reminder = get_effective_settings().mr_reminder
        assert mr_reminder.channels == (("acme/widget", "#widget-mrs"),)
