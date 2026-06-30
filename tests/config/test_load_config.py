"""``load_config`` settings parsing and ``Mode`` resolution.

Split verbatim from the former monolithic ``tests/test_config.py``
(souliane/teatree#443). Covers ``load_config`` defaults and per-key
parsing (workspace dir, branch prefix, privacy, agent-signature, the
human-approval gates, loop cadence) and the ``Mode`` parser plus its
toml/env resolution via ``get_effective_settings``.

Integration-first per the Test-Writing Doctrine: real TOML fixtures
under ``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched.
"""

import tomllib
from pathlib import Path

import pytest
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import Mode, OnBehalfPostMode, get_effective_settings, load_config
from teatree.core.models import ConfigSetting

from ._shared import _write_toml


def test_load_config_reads_toml_home_fields(tmp_path: Path) -> None:
    # privacy is TOML-home (read off the file). workspace_dir is DB-home now:
    # its ``[teatree]`` value is ignored on read so the field keeps its dataclass
    # default at the file tier (resolved per-overlay via config.worktree_root()).
    # review_skill is DB-home too and keeps its default at the file tier.
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[teatree]
workspace_dir = "/custom/workspace"
privacy = "strict"
""",
    )
    config = load_config(config_path)
    assert config.user.workspace_dir == Path.home() / "workspace"
    assert config.user.privacy == "strict"
    assert config.user.review_skill == ""
    assert "teatree" in config.raw


def test_load_config_missing_file(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.user.workspace_dir == Path.home() / "workspace"
    assert config.user.review_skill == ""
    assert config.user.privacy == ""


def test_load_config_defaults_when_teatree_section_empty(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[other]\nfoo = 1\n")
    config = load_config(config_path)
    assert config.user.review_skill == ""


def test_handover_mirror_path_defaults_under_xdg_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert (
        load_config(config_path).user.handover_mirror_path == tmp_path / "state" / "teatree" / "handover" / "latest.md"
    )


def test_handover_mirror_path_override(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, '[teatree]\nhandover_mirror_path = "/custom/ho.md"\n')
    assert load_config(config_path).user.handover_mirror_path == Path("/custom/ho.md")


def test_agent_signature_defaults_off(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.agent_signature is False


def test_toml_home_and_raw_keys_do_not_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # TOML-home carve-out fields, overlay discovery/messaging keys, and raw
    # bootstrap keys are legitimate in the file — they must NOT trip the WARN.
    # ``workspace_dir`` is deliberately ABSENT here — it is DB-home now and warns
    # on presence (see TestDbHomeTomlConflictWarning); this fixture covers only
    # the keys that must STAY silent.
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        '[teatree]\nprivacy = "strict"\n'
        'statusline_chain = []\nprivate_repos = ["acme/x"]\n\n'
        '[overlays.myproj]\npath = "~/p"\n',
    )
    with caplog.at_level("WARNING", logger="teatree.config"):
        load_config(config_path)
    warnings = "\n".join(r.getMessage() for r in caplog.records if "ignored on read" in r.getMessage().lower())
    assert warnings == ""


class TestDbHomeTomlConflictWarning(TestCase):
    """The #1775 DB-home-key-in-TOML warning is QUIET by default.

    After the migration cleaned the TOML of DB-homed keys, the load path used
    to emit one WARNING per DB-home key present in the file (~100 lines per
    command — unusable noise). The contract is now: a DB-home key that lives
    correctly in the DB (and is absent from / agrees with the TOML) is silent;
    a genuine conflict (the key set to a *different* value in BOTH the TOML and
    the DB store) warns, aggregated to a single line.
    """

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        self.caplog = caplog
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def _load_and_collect(self) -> list[str]:
        with self.caplog.at_level("WARNING", logger="teatree.config"):
            load_config(self.config_path)
        return [r.getMessage() for r in self.caplog.records if "ignored on read" in r.getMessage().lower()]

    def test_clean_db_home_config_produces_zero_warnings(self) -> None:
        # The core fix: a TOML that carries NO DB-home key (every DB-home setting
        # lives in the DB store, where it belongs) emits ZERO per-key warnings,
        # even with many DB rows present.
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("repo_mode", "solo")
        ConfigSetting.objects.set_value("contribute", value=True)
        # ``workspace_dir`` omitted: it is the one DB-home key that warns on
        # presence (it silently relocates worktrees) — covered separately below.
        _write_toml(self.config_path, '[teatree]\nprivacy = "strict"\n')
        assert self._load_and_collect() == []

    def test_retired_workspace_dir_in_toml_warns_to_migrate(self) -> None:
        # workspace_dir moved to the DB store (the per-overlay WORKTREE root); a
        # leftover ``[teatree] workspace_dir`` is ignored on read AND would
        # silently change where worktrees are created, so it ALWAYS warns (not
        # only on a value conflict) with the config_setting import migrate path.
        _write_toml(self.config_path, '[teatree]\nworkspace_dir = "/custom/ws"\n')
        warnings = self._load_and_collect()
        assert len(warnings) == 1
        assert "workspace_dir" in warnings[0]
        assert "config_setting import" in warnings[0]

    def test_overlay_scoped_workspace_dir_in_toml_warns(self) -> None:
        _write_toml(
            self.config_path,
            '[teatree]\n\n[overlays.myproj]\npath = "~/p"\nworkspace_dir = "/srv/ws"\n',
        )
        warnings = self._load_and_collect()
        assert len(warnings) == 1
        assert "workspace_dir" in warnings[0]
        assert "myproj" in warnings[0]

    def test_db_home_key_in_toml_without_db_row_is_silent(self) -> None:
        # A DB-home key still in the TOML but with NO DB row is being migrated
        # away — it is no longer a per-key warning (the noise the fix removes).
        _write_toml(self.config_path, '[teatree]\nmode = "auto"\nrepo_mode = "solo"\n')
        assert self._load_and_collect() == []

    def test_db_home_key_in_toml_agreeing_with_db_is_silent(self) -> None:
        # Present in BOTH toml and DB but the values AGREE — not a conflict, silent.
        ConfigSetting.objects.set_value("mode", "auto")
        _write_toml(self.config_path, '[teatree]\nmode = "auto"\n')
        assert self._load_and_collect() == []

    def test_genuine_conflict_warns_once(self) -> None:
        # Present in BOTH toml and DB with DIFFERING values: the TOML value is
        # silently ignored (the DB is its home), so the conflict is surfaced.
        ConfigSetting.objects.set_value("mode", "interactive")
        _write_toml(self.config_path, '[teatree]\nmode = "auto"\n')
        warnings = self._load_and_collect()
        # Aggregated to a single line — never one warning per offending key.
        assert len(warnings) == 1
        assert "mode" in warnings[0]
        assert "config_setting import" in warnings[0]

    def test_multiple_conflicts_aggregate_to_one_line(self) -> None:
        # Many conflicting keys collapse to ONE warning naming each key — never
        # ~100 lines, one per key (the regression this fix targets).
        ConfigSetting.objects.set_value("mode", "interactive")
        ConfigSetting.objects.set_value("repo_mode", "collaborative")
        ConfigSetting.objects.set_value("contribute", value=False)
        _write_toml(
            self.config_path,
            '[teatree]\nmode = "auto"\nrepo_mode = "solo"\ncontribute = true\n',
        )
        warnings = self._load_and_collect()
        assert len(warnings) == 1
        assert "mode" in warnings[0]
        assert "repo_mode" in warnings[0]
        assert "contribute" in warnings[0]

    def test_overlay_scoped_conflict_warns_once(self) -> None:
        # An overlay-scoped DB row conflicting with [overlays.<name>] is surfaced
        # against that overlay's DB scope, aggregated to a single line.
        ConfigSetting.objects.set_value("require_human_approval_to_merge", value=True, scope="myproj")
        _write_toml(
            self.config_path,
            '[teatree]\n\n[overlays.myproj]\npath = "~/p"\nrequire_human_approval_to_merge = false\n',
        )
        warnings = self._load_and_collect()
        assert len(warnings) == 1
        assert "require_human_approval_to_merge" in warnings[0]
        assert "myproj" in warnings[0]


class TestDbHomeGlobalResolution(TestCase):
    """DB-home settings resolve from a GLOBAL-scope ``ConfigSetting`` row.

    The DB twin of the old global ``[teatree] <key>``; an empty table resolves
    the dataclass default and a global row supplies the value.
    """

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        for env in ("T3_OVERLAY_NAME", "T3_MODE", "T3_ON_BEHALF_POST_MODE"):
            monkeypatch.delenv(env, raising=False)
        _write_toml(self.config_path, "[teatree]\n")

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


def test_orchestrator_bash_gate_enabled_defaults_on(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.orchestrator_bash_gate_enabled is True


def test_orchestrator_bash_gate_enabled_can_be_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\norchestrator_bash_gate_enabled = false\n")
    assert load_config(config_path).user.orchestrator_bash_gate_enabled is False


def test_require_human_approval_to_merge_defaults_on(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.require_human_approval_to_merge is True


def test_require_human_approval_to_answer_defaults_on(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.require_human_approval_to_answer is True


def test_notify_on_post_on_behalf_defaults_true(tmp_path: Path) -> None:
    """#949: after-receipt visibility DM ships ON by default."""
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.notify_on_post_on_behalf is True


def test_notify_on_post_on_behalf_no_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anti-vacuity guard: no copied-by-analogy ``T3_*`` env layer exists.

    ``notify_on_post_on_behalf`` is intentionally NOT in ``ENV_SETTING_OVERRIDES``
    (its sibling ``notify_user_via_bot`` has no env peer). An env var must
    therefore have ZERO effect.
    """
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    monkeypatch.setenv("T3_NOTIFY_ON_POST_ON_BEHALF", "false")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", config_path)
    assert get_effective_settings().notify_on_post_on_behalf is True


def test_on_behalf_post_mode_defaults_to_draft_or_ask(tmp_path: Path) -> None:
    """The dataclass-default mode at the file tier is DRAFT_OR_ASK (DB-home)."""
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK


def test_loop_cadence_seconds_defaults_to_720(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.loop_cadence_seconds == 720


def test_user_identity_aliases_defaults_empty(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.user_identity_aliases == []


def test_clean_ignore_defaults_empty(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.clean_ignore == []


def test_billing_cycle_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    user = load_config(config_path).user
    assert user.billing_cycle_anchor_day == 0
    assert user.sdk_monthly_credit_usd == pytest.approx(200.0)


class TestAutoloadSetting:
    """``autoload`` is TOML-home (#256): default OFF, ``[teatree] autoload`` enables, env wins.

    A cold-hook-readable TOML-home bool resolved from the ``[teatree]`` table +
    ``T3_AUTOLOAD`` env, never the DB store (the cold SessionStart hooks read it
    pre-Django with tomllib).
    """

    def test_default_is_off_with_empty_teatree_table(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.autoload is False

    def test_default_is_off_with_no_config_file(self, tmp_path: Path) -> None:
        assert load_config(tmp_path / "nonexistent.toml").user.autoload is False

    def test_toml_true_enables(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nautoload = true\n")
        assert load_config(config_path).user.autoload is True

    def test_quoted_string_true_does_not_engage(self, tmp_path: Path) -> None:
        # Strict bool (#256 nit): a QUOTED ``"true"`` is a string, not a bare TOML
        # boolean, so it must NOT engage autoload — matching the cold-read in
        # ``teatree_settings.autoload_enabled``. RED on ``bool(...)`` (truthy string).
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nautoload = "true"\n')
        assert load_config(config_path).user.autoload is False

    def test_quoted_string_false_does_not_engage(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nautoload = "false"\n')
        assert load_config(config_path).user.autoload is False

    def test_env_override_enables_over_empty_toml(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``T3_AUTOLOAD`` is the operational fast-toggle (env wins over the file tier)."""
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(config_file, "[teatree]\n")
        monkeypatch.setenv("T3_AUTOLOAD", "true")
        assert get_effective_settings().autoload is True

    def test_env_false_disables_over_toml_true(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(config_file, "[teatree]\nautoload = true\n")
        monkeypatch.setenv("T3_AUTOLOAD", "false")
        assert get_effective_settings().autoload is False


class TestIssueImplementerSettings:
    """Config surface for the opt-in, default-OFF issue-implementer loop (#1548).

    DB-home (#1775): the default at the file tier is OFF; an enable resolves from
    the ``ConfigSetting`` store, the DB twin of the old ``[teatree]`` global.
    """

    def test_defaults_are_opt_in_off(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        user = load_config(config_path).user
        assert user.issue_implementer_enabled is False
        assert user.issue_implementer_label == ""
        assert user.issue_implementer_max_concurrent == 1
        assert user.issue_implementer_cadence_hours == 1

    def test_env_kill_switch_applies(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``T3_ISSUE_IMPLEMENTER_ENABLED`` is the operational fast-disable (env wins)."""
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(config_file, "[teatree]\n")
        monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "false")

        assert get_effective_settings().issue_implementer_enabled is False

    def test_env_kill_switch_applies_without_toml(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del config_file, elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.setenv("T3_ISSUE_IMPLEMENTER_ENABLED", "true")

        assert get_effective_settings().issue_implementer_enabled is True


class TestOrchestrateClaimEnabledSetting:
    """``orchestrate_claim_enabled`` is DB-home (#1796/#1775): default OFF, env wins."""

    def test_default_is_opt_in_off(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.orchestrate_claim_enabled is False

    def test_env_override_enables(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``T3_ORCHESTRATE_CLAIM_ENABLED`` is the operational fast-toggle (env wins)."""
        del config_file, elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.setenv("T3_ORCHESTRATE_CLAIM_ENABLED", "true")
        assert get_effective_settings().orchestrate_claim_enabled is True


class TestTeamsEnabledSetting:
    """``teams_enabled`` is DB-home (#1838/#1775): default OFF (ships dark)."""

    def test_default_is_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_TEAMS_ENABLED", raising=False)
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        assert load_config(cfg).user.teams_enabled is False

    def test_default_is_off_with_no_config_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("T3_TEAMS_ENABLED", raising=False)
        assert load_config(tmp_path / "absent.toml").user.teams_enabled is False


class TestTeamsPaneBudgetSettings:
    """``teams_max_panes`` / ``teams_idle_minutes`` are DB-home (#1838/#1775).

    The file-tier default is the conservative bound; the value resolves from a
    ``ConfigSetting`` row (a non-positive/non-int stored value fails safe to the
    default via the registry parser).
    """

    def test_max_panes_default(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        assert load_config(cfg).user.teams_max_panes == 1

    def test_idle_minutes_default(self, tmp_path: Path) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\n", encoding="utf-8")
        assert load_config(cfg).user.teams_idle_minutes == 30

    def test_defaults_with_no_config_file(self, tmp_path: Path) -> None:
        settings = load_config(tmp_path / "absent.toml").user
        assert settings.teams_max_panes == 1
        assert settings.teams_idle_minutes == 30


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


class TestRequireReviewContextSetting:
    """``require_review_context`` is DB-home (#1775): default OFF at the file tier."""

    def test_default_is_off(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.require_review_context is False


class TestE2EConfidenceThresholdSetting:
    """``e2e_confidence_threshold`` is DB-home (#1775): default 90 at the file tier.

    The DB-tier resolution (global + overlay rows) is covered in
    ``test_overlay_overrides.py``; this pins the file-tier default.
    """

    def test_default_is_90(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.e2e_confidence_threshold == 90

    def test_default_with_no_config_file(self, tmp_path: Path) -> None:
        assert load_config(tmp_path / "absent.toml").user.e2e_confidence_threshold == 90


class TestAutoUpdateSettings:
    """#1760: CI-green gate + deferred-reinstall flags are DB-home (#1775); env wins."""

    def test_require_green_main_defaults_on(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.auto_update_require_green_main is True

    def test_reinstall_defaults_off(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.auto_update_reinstall is False

    def test_env_override_enables_reinstall(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # T3_LOOP_AUTO_UPDATE wins over the toml so the opt-in can be flipped
        # on for one run without editing ~/.teatree.toml.
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(config_file, "[teatree]\n")
        monkeypatch.setenv("T3_LOOP_AUTO_UPDATE", "true")

        assert get_effective_settings().auto_update_reinstall is True


class TestMode:
    """Parse and resolution of the ``t3.mode`` setting.

    The default must stay conservative (INTERACTIVE): auto mode grants the
    agent end-to-end autonomy including publishing actions, so a typo in the
    config must never silently downgrade to it.
    """

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

    def test_load_config_default_is_interactive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        config = load_config(tmp_path / "nonexistent.toml")
        assert config.user.mode is Mode.INTERACTIVE

    def test_env_var_overrides_db(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T3_MODE wins over the resolved value — verified via get_effective_settings."""
        del elsewhere, no_installed_overlays
        _write_toml(config_file, "[teatree]\n")
        monkeypatch.setenv("T3_MODE", "interactive")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

        assert get_effective_settings().mode is Mode.INTERACTIVE

    def test_env_var_applies_without_toml(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T3_MODE applies even when no toml file exists."""
        del config_file, elsewhere, no_installed_overlays
        monkeypatch.setenv("T3_MODE", "auto")
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

        assert get_effective_settings().mode is Mode.AUTO

    def test_load_config_malformed_toml_raises_named_error(self, tmp_path: Path) -> None:
        # #1652: a TOML syntax error surfaces as a typed, message-bearing
        # config error naming the file, never a raw TOMLDecodeError traceback.
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nworkspace_dir = \n")

        with pytest.raises(ValueError, match="Malformed TOML") as exc_info:
            load_config(config_path)
        assert str(config_path) in str(exc_info.value)
        assert not isinstance(exc_info.value, tomllib.TOMLDecodeError)


class TestModeDbResolution(TestCase):
    """``mode`` is DB-home (#1775): it resolves from a ``ConfigSetting`` row."""

    @pytest.fixture(autouse=True)
    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(self.config_path, "[teatree]\n")

    def test_global_db_row_reflects(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert get_effective_settings().mode is Mode.AUTO
        ConfigSetting.objects.set_value("mode", "interactive")
        assert get_effective_settings().mode is Mode.INTERACTIVE

    def test_corrupt_db_value_raises_loud_on_read(self) -> None:
        ConfigSetting.objects.set_value("mode", "headless")
        with pytest.raises(ValueError, match="mode"):
            get_effective_settings()
