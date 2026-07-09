# test-path: cross-cutting
"""Per-overlay override machinery under the #1775 DB partition.

A DB-home setting is per-overlay overridable via a ``ConfigSetting`` row scoped to
the overlay (``scope=<overlay name>``). The resolution chain (later wins): env ->
overlay-scoped DB row -> global DB row -> dataclass default. A DB-home key placed
inside an ``overlays`` registry entry is parsed by discovery but dropped on read
(its sole home is a scoped ``ConfigSetting`` row).

Integration-first per the Test-Writing Doctrine: DB-home overrides via the real
``ConfigSetting`` store asserted through ``get_effective_settings``; the overlays
registry seeded into the cold-path sqlite (``config_db``).
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.config import Mode, discover_overlays, get_effective_settings
from teatree.core.models import ConfigSetting

from ._shared import _seed_config_db


class TestOverlayRegistryParsing:
    """An ``overlays`` registry entry's per-overlay override is parsed by discovery."""

    @pytest.mark.usefixtures("no_installed_overlays")
    def test_overlay_mode_parsed_into_entry(self, config_db: Path) -> None:
        # ``mode`` is DB-home, so discovery parses it from the registry entry into
        # the overrides dict (the resolver drops it on read — see below); discovery
        # itself still coerces the value.
        _seed_config_db(config_db, overlays={"my-overlay": {"class": "x.y:Z", "mode": "auto"}})
        by_name = {e.name: e for e in discover_overlays()}
        assert by_name["my-overlay"].overrides["mode"] is Mode.AUTO

    @pytest.mark.usefixtures("no_installed_overlays")
    def test_overlay_invalid_mode_raises(self, config_db: Path) -> None:
        _seed_config_db(config_db, overlays={"my-overlay": {"class": "x.y:Z", "mode": "nope"}})
        with pytest.raises(ValueError, match="Invalid t3 mode"):
            discover_overlays()

    @pytest.mark.usefixtures("no_installed_overlays")
    def test_db_home_key_in_registry_entry_is_dropped_on_read(
        self, config_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A DB-home key inside the overlays registry entry is IGNORED on read (its
        # home is a scoped ConfigSetting row); with none the dataclass default stands.
        monkeypatch.setenv("T3_OVERLAY_NAME", "looseshell")
        _seed_config_db(
            config_db,
            overlays={"looseshell": {"class": "x.y:Z", "orchestrator_bash_gate_enabled": False}},
        )
        assert get_effective_settings().orchestrator_bash_gate_enabled is True

    @pytest.mark.usefixtures("no_installed_overlays")
    def test_privacy_registry_entry_value_is_dropped_on_read(
        self, config_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "client")
        _seed_config_db(config_db, overlays={"client": {"class": "x.y:Z", "privacy": "strict"}})
        assert get_effective_settings().privacy == ""


class TestOverlayDbHomeOverrides(TestCase):
    """DB-home per-overlay overrides via an overlay-scoped ``ConfigSetting`` row.

    A row scoped to the active overlay beats the global DB row; the active overlay
    is resolved from ``T3_OVERLAY_NAME``.
    """

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in (
            "T3_MODE",
            "T3_OVERLAY_NAME",
            "T3_ISSUE_IMPLEMENTER_ENABLED",
            "T3_ORCHESTRATE_CLAIM_ENABLED",
            "T3_TEAMS_ENABLED",
            "T3_TEAMS_MAX_PANES",
            "T3_TEAMS_IDLE_MINUTES",
        ):
            monkeypatch.delenv(env, raising=False)
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

    def test_overlay_can_override_orchestrator_bash_gate_enabled(self) -> None:
        ConfigSetting.objects.set_value("orchestrator_bash_gate_enabled", value=False, scope="my-overlay")
        self._activate()
        assert get_effective_settings().orchestrator_bash_gate_enabled is False

    def test_overlay_can_override_privacy(self) -> None:
        ConfigSetting.objects.set_value("privacy", "strict", scope="my-overlay")
        self._activate()
        assert get_effective_settings().privacy == "strict"

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
        assert get_effective_settings().e2e_mandatory_gate_enabled is True
        ConfigSetting.objects.set_value("e2e_mandatory_gate_enabled", value=False, scope="my-overlay")
        self._activate()
        assert get_effective_settings().e2e_mandatory_gate_enabled is False

    def test_pane_budget_env_non_positive_fails_safe(self) -> None:
        self.monkeypatch.setenv("T3_TEAMS_MAX_PANES", "0")
        self.monkeypatch.setenv("T3_TEAMS_IDLE_MINUTES", "garbage")
        settings = get_effective_settings()
        assert settings.teams_max_panes == 1
        assert settings.teams_idle_minutes == 30


class TestAliasScopeGroupsMerge(TestCase):
    """Canonically-equivalent scope groups MERGE for the active overlay.

    Rows may be scoped under the short alias (``my``) or the ``t3-``-prefixed
    entry-point name (``t3-my``). Both address the same overlay, so the resolver
    unions them; on a key collision the exact-name row wins over an alias row.
    """

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_MODE", "T3_OVERLAY_NAME"):
            monkeypatch.delenv(env, raising=False)
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-my")

    def test_alias_scoped_rows_merge_with_exact_scoped_rows(self) -> None:
        ConfigSetting.objects.set_value("review_skill", "xp", scope="t3-my")
        ConfigSetting.objects.set_value("ban_close_trailers_on_namespaces", ["grp/*"], scope="my")
        settings = get_effective_settings()
        assert settings.review_skill == "xp"
        assert settings.ban_close_trailers_on_namespaces == ["grp/*"]

    def test_exact_scope_wins_over_alias_scope_on_collision(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto", scope="t3-my")
        ConfigSetting.objects.set_value("mode", "interactive", scope="my")
        assert get_effective_settings().mode is Mode.AUTO

    def test_alias_only_rows_resolve_when_exact_group_also_exists(self) -> None:
        ConfigSetting.objects.set_value("review_skill", "xp", scope="my")
        ConfigSetting.objects.set_value("mode", "auto", scope="t3-my")
        settings = get_effective_settings()
        assert settings.review_skill == "xp"
        assert settings.mode is Mode.AUTO
