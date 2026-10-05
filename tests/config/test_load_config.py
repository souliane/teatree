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

from teatree.config import Mode, get_effective_settings, load_config
from teatree.config.setting_parsers import _parse_overridable_positive_int
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
            "T3_AUTOLOAD",
        ):
            monkeypatch.delenv(env, raising=False)

    def test_security_and_autonomy_gate_defaults(self) -> None:
        settings = get_effective_settings()
        assert settings.mode is Mode.AUTO
        assert settings.require_human_approval_to_answer is False
        assert settings.require_human_approval_to_merge is True
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

    def test_issue_implementer_defaults(self) -> None:
        settings = get_effective_settings()
        assert settings.issue_implementer_label == ""
        assert settings.issue_implementer_max_concurrent == 3

    def test_auto_update_requires_a_green_default_branch(self) -> None:
        assert get_effective_settings().auto_update_require_green_main is True


def test_handover_mirror_path_defaults_under_xdg_data(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # #3563: the mirror lives under the SHARED data dir (bind-mounted), not the XDG state dir.
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    monkeypatch.delenv("T3_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert get_effective_settings().handover_mirror_path == tmp_path / "data" / "teatree" / "handover" / "latest.md"


class TestDbTierGlobalResolution(TestCase):
    """DB-home settings resolve from a GLOBAL-scope ``ConfigSetting`` row."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_MODE"):
            monkeypatch.delenv(env, raising=False)

    def test_require_human_approval_to_merge_db_disable(self) -> None:
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=False)
        assert get_effective_settings().require_human_approval_to_merge is False

    def test_require_human_approval_to_answer_db_disable(self) -> None:
        ConfigSetting.objects.set_value("require_human_approval_to_answer", value=False)
        assert get_effective_settings().require_human_approval_to_answer is False

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


class TestModeDbResolution(TestCase):
    """``mode`` resolves from a ``ConfigSetting`` row; a corrupt value raises loud."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_global_db_row_reflects(self) -> None:
        # ``babysit`` disables the autonomy collapse so the raw global ``mode`` row
        # is observable (a ``full`` overlay pins ``mode = auto`` regardless).
        ConfigSetting.objects.set_value("autonomy", "babysit")
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


class TestPositiveIntParser:
    """Pure-logic coverage of the fail-safe overridable positive-int coercer."""

    def test_overridable_parser_accepts_positive_int(self) -> None:

        assert _parse_overridable_positive_int(1)(5) == 5

    def test_overridable_parser_rejects_bool(self) -> None:

        # ``bool`` is an ``int`` subclass — it must NOT slip through as 1/0.
        parse = _parse_overridable_positive_int(1)
        truthy: object = True
        falsy: object = False
        assert parse(truthy) == 1
        assert parse(falsy) == 1

    def test_overridable_parser_non_positive_int_fails_safe(self) -> None:

        assert _parse_overridable_positive_int(3)(0) == 3
        assert _parse_overridable_positive_int(3)(-1) == 3

    def test_overridable_parser_accepts_numeric_string(self) -> None:

        # The DB tier may store ``"6"``; a positive numeric string is honoured.
        assert _parse_overridable_positive_int(1)("6") == 6
        assert _parse_overridable_positive_int(1)("0") == 1

    def test_overridable_parser_non_numeric_string_fails_safe(self) -> None:

        assert _parse_overridable_positive_int(4)("lots") == 4

    def test_overridable_parser_other_types_fail_safe(self) -> None:

        assert _parse_overridable_positive_int(2)([3]) == 2
        assert _parse_overridable_positive_int(2)(None) == 2
