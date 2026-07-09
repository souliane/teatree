"""``load_config`` (DB-home) + DB-tier settings resolution + ``Mode`` parsing.

Every ``UserSettings`` field is DB-home: ``load_config`` reads no file, so
``.user`` is always the dataclass defaults and ``.raw`` carries only the
``overlays`` / ``e2e_repos`` registries (read from the DB). Effective values
resolve from the ``ConfigSetting`` store + the ``T3_*`` env layer via
``get_effective_settings`` — the contract these tests exercise.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.config import Mode, OnBehalfPostMode, get_effective_settings, load_config
from teatree.config.settings import UserSettings
from teatree.core.models import ConfigSetting

from ._shared import _seed_config_db


def test_load_config_user_is_dataclass_defaults() -> None:
    assert load_config().user == UserSettings()


def test_load_config_raw_carries_db_registries(config_db: Path) -> None:
    _seed_config_db(
        config_db,
        overlays={"db-overlay": {"class": "dbpkg.settings"}},
        e2e_repos={"myrepo": {"url": "git@x:r.git"}},
    )
    raw = load_config().raw
    assert raw["overlays"] == {"db-overlay": {"class": "dbpkg.settings"}}
    assert raw["e2e_repos"] == {"myrepo": {"url": "git@x:r.git"}}


class TestDbTierDefaults(TestCase):
    """With an empty ``ConfigSetting`` store, every DB-home field resolves to its default."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in (
            "T3_OVERLAY_NAME",
            "T3_MODE",
            "T3_ON_BEHALF_POST_MODE",
            "T3_AUTOLOAD",
            "T3_ISSUE_IMPLEMENTER_ENABLED",
            "T3_ORCHESTRATE_CLAIM_ENABLED",
            "T3_TEAMS_ENABLED",
            "T3_NOTIFY_ON_POST_ON_BEHALF",
            "T3_LOOP_AUTO_UPDATE",
        ):
            monkeypatch.delenv(env, raising=False)

    def test_security_and_autonomy_gate_defaults(self) -> None:
        settings = get_effective_settings()
        assert settings.mode is Mode.INTERACTIVE
        assert settings.require_human_approval_to_merge is True
        assert settings.require_human_approval_to_answer is True
        assert settings.notify_on_post_on_behalf is True
        assert settings.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK
        assert settings.agent_signature is False
        assert settings.orchestrator_bash_gate_enabled is True

    def test_loop_and_list_defaults(self) -> None:
        settings = get_effective_settings()
        assert settings.loop_cadence_seconds == 720
        assert settings.user_identity_aliases == []
        assert settings.clean_ignore == []

    def test_billing_defaults(self) -> None:
        settings = get_effective_settings()
        assert settings.billing_cycle_anchor_day == 0
        assert settings.sdk_monthly_credit_usd == pytest.approx(200.0)

    def test_opt_in_flag_defaults_off(self) -> None:
        settings = get_effective_settings()
        assert settings.autoload is False
        assert settings.orchestrate_claim_enabled is False
        assert settings.teams_enabled is False
        assert settings.require_review_context is False

    def test_teams_pane_budget_defaults(self) -> None:
        settings = get_effective_settings()
        assert settings.teams_max_panes == 1
        assert settings.teams_idle_minutes == 30

    def test_issue_implementer_defaults_are_opt_in_off(self) -> None:
        settings = get_effective_settings()
        assert settings.issue_implementer_enabled is False
        assert settings.issue_implementer_label == ""
        assert settings.issue_implementer_max_concurrent == 1
        assert settings.issue_implementer_cadence_hours == 1

    def test_e2e_confidence_threshold_default(self) -> None:
        assert get_effective_settings().e2e_confidence_threshold == 90

    def test_auto_update_defaults(self) -> None:
        settings = get_effective_settings()
        assert settings.auto_update_require_green_main is True
        assert settings.auto_update_reinstall is False


def test_handover_mirror_path_defaults_under_xdg_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert get_effective_settings().handover_mirror_path == tmp_path / "state" / "teatree" / "handover" / "latest.md"


class TestDbTierGlobalResolution(TestCase):
    """DB-home settings resolve from a GLOBAL-scope ``ConfigSetting`` row."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_MODE", "T3_ON_BEHALF_POST_MODE"):
            monkeypatch.delenv(env, raising=False)

    def test_agent_signature_db_opt_in(self) -> None:
        ConfigSetting.objects.set_value("agent_signature", value=True)
        assert get_effective_settings().agent_signature is True

    def test_require_human_approval_to_merge_db_disable(self) -> None:
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=False)
        assert get_effective_settings().require_human_approval_to_merge is False

    def test_require_human_approval_to_answer_db_disable(self) -> None:
        ConfigSetting.objects.set_value("require_human_approval_to_answer", value=False)
        assert get_effective_settings().require_human_approval_to_answer is False

    def test_notify_on_post_on_behalf_db_false(self) -> None:
        ConfigSetting.objects.set_value("notify_on_post_on_behalf", value=False)
        assert get_effective_settings().notify_on_post_on_behalf is False

    def test_on_behalf_post_mode_db_immediate(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE

    def test_on_behalf_post_mode_db_ask(self) -> None:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.ASK

    def test_user_identity_aliases_db(self) -> None:
        ConfigSetting.objects.set_value("user_identity_aliases", ["adrien.work", "souliane", "adrien.cossa"])
        assert get_effective_settings().user_identity_aliases == ["adrien.work", "souliane", "adrien.cossa"]

    def test_clean_ignore_db(self) -> None:
        ConfigSetting.objects.set_value("clean_ignore", ["spike/*", "dev-override"])
        assert get_effective_settings().clean_ignore == ["spike/*", "dev-override"]

    def test_loop_cadence_seconds_db(self) -> None:
        ConfigSetting.objects.set_value("loop_cadence_seconds", value=300)
        assert get_effective_settings().loop_cadence_seconds == 300

    def test_billing_cycle_anchor_and_credit_db(self) -> None:
        ConfigSetting.objects.set_value("billing_cycle_anchor_day", value=15)
        ConfigSetting.objects.set_value("sdk_monthly_credit_usd", value=100.0)
        settings = get_effective_settings()
        assert settings.billing_cycle_anchor_day == 15
        assert settings.sdk_monthly_credit_usd == pytest.approx(100.0)


class TestEnvOverrides(TestCase):
    """The ``T3_*`` env layer is the highest tier — it wins over the DB-home default."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        self.monkeypatch = monkeypatch

    def test_mode_env_applies(self) -> None:
        self.monkeypatch.setenv("T3_MODE", "auto")
        assert get_effective_settings().mode is Mode.AUTO

    def test_autoload_env_enables(self) -> None:
        self.monkeypatch.setenv("T3_AUTOLOAD", "true")
        assert get_effective_settings().autoload is True

    def test_issue_implementer_env_enables(self) -> None:
        self.monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "true")
        assert get_effective_settings().issue_implementer_enabled is True

    def test_orchestrate_claim_env_enables(self) -> None:
        self.monkeypatch.setenv("T3_ORCHESTRATE_CLAIM_ENABLED", "true")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_auto_update_reinstall_env_enables(self) -> None:
        self.monkeypatch.setenv("T3_LOOP_AUTO_UPDATE", "true")
        assert get_effective_settings().auto_update_reinstall is True

    def test_notify_on_post_on_behalf_has_no_env_layer(self) -> None:
        # Anti-vacuity guard: ``notify_on_post_on_behalf`` is intentionally NOT in
        # ENV_SETTING_OVERRIDES, so an env var has ZERO effect (stays default True).
        self.monkeypatch.setenv("T3_NOTIFY_ON_POST_ON_BEHALF", "false")
        assert get_effective_settings().notify_on_post_on_behalf is True


class TestModeDbResolution(TestCase):
    """``mode`` resolves from a ``ConfigSetting`` row; a corrupt value raises loud."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_global_db_row_reflects(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert get_effective_settings().mode is Mode.AUTO
        ConfigSetting.objects.set_value("mode", "interactive")
        assert get_effective_settings().mode is Mode.INTERACTIVE

    def test_corrupt_db_value_raises_loud_on_read(self) -> None:
        ConfigSetting.objects.set_value("mode", "headless")
        with pytest.raises(ValueError, match="mode"):
            get_effective_settings()


class TestModeParse:
    """Parse of the ``Mode`` setting — the default stays conservative (INTERACTIVE)."""

    def test_parse_interactive(self) -> None:
        assert Mode.parse("interactive") is Mode.INTERACTIVE

    def test_parse_auto(self) -> None:
        assert Mode.parse("auto") is Mode.AUTO

    def test_parse_is_case_insensitive(self) -> None:
        assert Mode.parse("AUTO") is Mode.AUTO
        assert Mode.parse("  Interactive  ") is Mode.INTERACTIVE

    def test_parse_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid t3 mode"):
            Mode.parse("headless")


class TestPaneBudgetParsers:
    """Pure-logic coverage of the fail-safe positive-int coercers (#1838 PR#7a)."""

    def test_env_parser_accepts_positive_int(self) -> None:
        from teatree.config.settings import _parse_env_positive_int  # noqa: PLC0415

        assert _parse_env_positive_int(1)("4") == 4

    def test_env_parser_fails_safe_on_non_positive(self) -> None:
        from teatree.config.settings import _parse_env_positive_int  # noqa: PLC0415

        parse = _parse_env_positive_int(7)
        assert parse("0") == 7
        assert parse("-3") == 7

    def test_env_parser_fails_safe_on_non_int(self) -> None:
        from teatree.config.settings import _parse_env_positive_int  # noqa: PLC0415

        parse = _parse_env_positive_int(9)
        assert parse("garbage") == 9
        assert parse("") == 9

    def test_overridable_parser_accepts_positive_int(self) -> None:
        from teatree.config.settings import _parse_overridable_positive_int  # noqa: PLC0415

        assert _parse_overridable_positive_int(1)(5) == 5

    def test_overridable_parser_rejects_bool(self) -> None:
        from teatree.config.settings import _parse_overridable_positive_int  # noqa: PLC0415

        # ``bool`` is an ``int`` subclass — it must NOT slip through as 1/0.
        parse = _parse_overridable_positive_int(1)
        truthy: object = True
        falsy: object = False
        assert parse(truthy) == 1
        assert parse(falsy) == 1

    def test_overridable_parser_non_positive_int_fails_safe(self) -> None:
        from teatree.config.settings import _parse_overridable_positive_int  # noqa: PLC0415

        assert _parse_overridable_positive_int(3)(0) == 3
        assert _parse_overridable_positive_int(3)(-1) == 3

    def test_overridable_parser_accepts_numeric_string(self) -> None:
        from teatree.config.settings import _parse_overridable_positive_int  # noqa: PLC0415

        # The DB tier may store ``"6"``; a positive numeric string is honoured.
        assert _parse_overridable_positive_int(1)("6") == 6
        assert _parse_overridable_positive_int(1)("0") == 1

    def test_overridable_parser_non_numeric_string_fails_safe(self) -> None:
        from teatree.config.settings import _parse_overridable_positive_int  # noqa: PLC0415

        assert _parse_overridable_positive_int(4)("lots") == 4

    def test_overridable_parser_other_types_fail_safe(self) -> None:
        from teatree.config.settings import _parse_overridable_positive_int  # noqa: PLC0415

        assert _parse_overridable_positive_int(2)([3]) == 2
        assert _parse_overridable_positive_int(2)(None) == 2
