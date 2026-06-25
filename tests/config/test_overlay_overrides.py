# test-path: cross-cutting
"""Per-overlay override machinery under the #1775 DB/TOML hard partition.

A DB-home setting is per-overlay overridable via a ``ConfigSetting`` row scoped
to the overlay (``scope=<overlay name>``); a TOML-home setting via
``[overlays.<name>]``. The resolution chain (later wins): env →
overlay-scoped DB / per-overlay TOML → global DB / global TOML → dataclass
default. A ``[overlays.<name>]`` value for a DB-home key is ignored on read.

Integration-first per the Test-Writing Doctrine: real TOML under ``tmp_path``
with ``teatree.config.CONFIG_PATH`` monkeypatched, DB-home overrides via the real
``ConfigSetting`` store, asserted through the real ``get_effective_settings``.
"""

from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import Mode, discover_overlays, get_effective_settings
from teatree.core.models import ConfigSetting

from ._shared import _write_toml


class TestOverlayTomlOverrides:
    """TOML-home per-overlay overrides via ``[overlays.<name>]``."""

    def test_overlay_toml_mode_parsed_into_entry(self, tmp_path: Path) -> None:
        # ``mode`` is DB-home, so discovery parses it from [overlays.<name>] into
        # the entry overrides dict (the union parse), but the resolver drops it on
        # read — discovery itself still coerces the value.
        config_path = tmp_path / ".teatree.toml"
        _write_toml(
            config_path,
            '[overlays.my-overlay]\nclass = "x.y:Z"\nmode = "auto"\n',
        )
        entries = discover_overlays(config_path=config_path)
        by_name = {e.name: e for e in entries}
        assert by_name["my-overlay"].overrides["mode"] is Mode.AUTO

    def test_overlay_invalid_mode_raises(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[overlays.my-overlay]\nclass = "x.y:Z"\nmode = "nope"\n')
        with pytest.raises(ValueError, match="Invalid t3 mode"):
            discover_overlays(config_path=config_path)

    def test_orchestrator_bash_gate_overlay_toml_override(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # orchestrator_bash_gate_enabled is TOML-home: a [overlays.<name>] value
        # wins over the global [teatree] value.
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "looseshell")
        _write_toml(
            config_file,
            "[teatree]\norchestrator_bash_gate_enabled = true\n\n"
            '[overlays.looseshell]\nclass = "x.y:Z"\norchestrator_bash_gate_enabled = false\n',
        )
        assert get_effective_settings().orchestrator_bash_gate_enabled is False

    def test_privacy_overlay_toml_override(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        _write_toml(
            config_file,
            '[teatree]\nprivacy = "loose"\n\n[overlays.client]\nclass = "x.y:Z"\nprivacy = "strict"\n',
        )
        assert get_effective_settings().privacy == "strict"


class TestOverlayDbHomeOverrides(TestCase):
    """DB-home per-overlay overrides via an overlay-scoped ``ConfigSetting`` row.

    The DB twin of the old ``[overlays.<name>]`` TOML override: a row scoped to
    the active overlay beats the global DB row, and a ``[overlays.<name>]`` TOML
    value for the same DB-home key is ignored on read.
    """

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        for env in (
            "T3_MODE",
            "T3_OVERLAY_NAME",
            "T3_ISSUE_IMPLEMENTER_ENABLED",
            "T3_ORCHESTRATE_CLAIM_ENABLED",
            "T3_DEDICATED_LOOPS",
            "T3_TEAMS_ENABLED",
            "T3_TEAMS_MAX_PANES",
            "T3_TEAMS_IDLE_MINUTES",
        ):
            monkeypatch.delenv(env, raising=False)
        _write_toml(self.config_path, '[teatree]\n\n[overlays.my-overlay]\nclass = "x.y:Z"\n')
        self.monkeypatch = monkeypatch

    def _activate(self) -> None:
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")

    def test_overlay_scoped_mode_wins_over_global(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive")
        ConfigSetting.objects.set_value("mode", "auto", scope="my-overlay")
        self._activate()
        assert get_effective_settings().mode is Mode.AUTO

    def test_overlay_scoped_str_setting_wins_over_global(self) -> None:
        ConfigSetting.objects.set_value("review_skill", "ac")
        ConfigSetting.objects.set_value("review_skill", "xp", scope="my-overlay")
        self._activate()
        assert get_effective_settings().review_skill == "xp"

    def test_env_var_beats_overlay_scoped_db_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto", scope="my-overlay")
        self.monkeypatch.setenv("T3_MODE", "interactive")
        self._activate()
        assert get_effective_settings().mode is Mode.INTERACTIVE

    def test_overlay_inherits_global_db_when_no_scoped_row(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        self._activate()
        assert get_effective_settings().mode is Mode.AUTO

    def test_overlay_can_override_user_identity_aliases(self) -> None:
        ConfigSetting.objects.set_value("user_identity_aliases", ["souliane"])
        ConfigSetting.objects.set_value(
            "user_identity_aliases", ["adrien.work", "souliane", "adrien.cossa"], scope="my-overlay"
        )
        self._activate()
        assert get_effective_settings().user_identity_aliases == ["adrien.work", "souliane", "adrien.cossa"]

    def test_overlay_can_override_clean_ignore(self) -> None:
        ConfigSetting.objects.set_value("clean_ignore", ["global-*"])
        ConfigSetting.objects.set_value("clean_ignore", ["spike/*", "dev-override"], scope="my-overlay")
        self._activate()
        assert get_effective_settings().clean_ignore == ["spike/*", "dev-override"]

    def test_overlay_can_override_require_human_approval_to_answer(self) -> None:
        ConfigSetting.objects.set_value("require_human_approval_to_answer", value=True)
        ConfigSetting.objects.set_value("require_human_approval_to_answer", value=False, scope="my-overlay")
        self._activate()
        assert get_effective_settings().require_human_approval_to_answer is False

    def test_overlay_can_override_notify_user_via_bot(self) -> None:
        ConfigSetting.objects.set_value("notify_user_via_bot", value=False, scope="my-overlay")
        self._activate()
        assert get_effective_settings().notify_user_via_bot is False

    def test_overlay_can_override_notify_on_post_on_behalf(self) -> None:
        ConfigSetting.objects.set_value("notify_on_post_on_behalf", value=False, scope="my-overlay")
        self._activate()
        assert get_effective_settings().notify_on_post_on_behalf is False

    def test_overlay_can_override_require_review_context(self) -> None:
        ConfigSetting.objects.set_value("require_review_context", value=True, scope="my-overlay")
        self._activate()
        assert get_effective_settings().require_review_context is True

    def test_overlay_can_override_require_rubric_verification(self) -> None:
        ConfigSetting.objects.set_value("require_rubric_verification", value=True, scope="my-overlay")
        self._activate()
        assert get_effective_settings().require_rubric_verification is True

    def test_overlay_can_override_require_spec_coverage(self) -> None:
        ConfigSetting.objects.set_value("require_spec_coverage", value=True, scope="my-overlay")
        self._activate()
        assert get_effective_settings().require_spec_coverage is True

    def test_overlay_can_override_max_concurrent_local_stacks(self) -> None:
        ConfigSetting.objects.set_value("max_concurrent_local_stacks", value=1, scope="my-overlay")
        self._activate()
        assert get_effective_settings().max_concurrent_local_stacks == 1

    def test_overlay_can_override_orchestrate_claim_enabled(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        self._activate()
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_can_override_issue_implementer_settings(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        ConfigSetting.objects.set_value("issue_implementer_label", "auto-implement", scope="my-overlay")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", value=3, scope="my-overlay")
        self._activate()
        effective = get_effective_settings()
        assert effective.issue_implementer_enabled is True
        assert effective.issue_implementer_label == "auto-implement"
        assert effective.issue_implementer_max_concurrent == 3

    def test_env_kill_switch_beats_overlay_db_override(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="my-overlay")
        self.monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "false")
        self._activate()
        assert get_effective_settings().issue_implementer_enabled is False

    def test_overlay_can_override_dedicated_loops(self) -> None:
        ConfigSetting.objects.set_value("dedicated_loops", value=True, scope="my-overlay")
        self._activate()
        assert get_effective_settings().dedicated_loops is True

    def test_env_dedicated_loops_beats_overlay_db_override(self) -> None:
        ConfigSetting.objects.set_value("dedicated_loops", value=True, scope="my-overlay")
        self.monkeypatch.setenv("T3_DEDICATED_LOOPS", "false")
        self._activate()
        assert get_effective_settings().dedicated_loops is False

    def test_overlay_can_override_teams_enabled(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=True, scope="my-overlay")
        self._activate()
        assert get_effective_settings().teams_enabled is True

    def test_env_teams_enabled_beats_overlay_db_override(self) -> None:
        ConfigSetting.objects.set_value("teams_enabled", value=True, scope="my-overlay")
        self.monkeypatch.setenv("T3_TEAMS_ENABLED", "false")
        self._activate()
        assert get_effective_settings().teams_enabled is False

    def test_overlay_can_override_pane_budget(self) -> None:
        ConfigSetting.objects.set_value("teams_max_panes", value=4, scope="my-overlay")
        ConfigSetting.objects.set_value("teams_idle_minutes", value=60, scope="my-overlay")
        self._activate()
        settings = get_effective_settings()
        assert settings.teams_max_panes == 4
        assert settings.teams_idle_minutes == 60

    def test_env_pane_budget_beats_overlay_db_override(self) -> None:
        ConfigSetting.objects.set_value("teams_max_panes", value=9, scope="my-overlay")
        ConfigSetting.objects.set_value("teams_idle_minutes", value=90, scope="my-overlay")
        self.monkeypatch.setenv("T3_TEAMS_MAX_PANES", "2")
        self.monkeypatch.setenv("T3_TEAMS_IDLE_MINUTES", "15")
        self._activate()
        settings = get_effective_settings()
        assert settings.teams_max_panes == 2
        assert settings.teams_idle_minutes == 15

    def test_overlay_can_override_mr_title_regex(self) -> None:
        ConfigSetting.objects.set_value("mr_title_regex", r"^JIRA-\d+: .+", scope="my-overlay")
        self._activate()
        assert get_effective_settings().mr_title_regex == r"^JIRA-\d+: .+"

    def test_e2e_mandatory_gate_default_on_and_overlay_can_disable(self) -> None:
        # Default ON with no rows.
        assert get_effective_settings().e2e_mandatory_gate_enabled is True
        ConfigSetting.objects.set_value("e2e_mandatory_gate_enabled", value=False, scope="my-overlay")
        self._activate()
        assert get_effective_settings().e2e_mandatory_gate_enabled is False

    def test_db_home_overlay_toml_value_is_ignored(self) -> None:
        # A [overlays.<name>] DB-home value is parsed by discovery but ignored on
        # read — the dataclass default stands without a DB row.
        _write_toml(
            self.config_path,
            '[teatree]\n\n[overlays.my-overlay]\nclass = "x.y:Z"\nissue_implementer_enabled = true\n',
        )
        self._activate()
        assert get_effective_settings().issue_implementer_enabled is False

    def test_pane_budget_env_non_positive_fails_safe(self) -> None:
        self.monkeypatch.setenv("T3_TEAMS_MAX_PANES", "0")
        self.monkeypatch.setenv("T3_TEAMS_IDLE_MINUTES", "garbage")
        settings = get_effective_settings()
        assert settings.teams_max_panes == 1
        assert settings.teams_idle_minutes == 30
