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

from teatree.config import Mode, get_effective_settings, load_config

from ._shared import _write_toml


def test_load_config_from_file(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        """
[teatree]
workspace_dir = "/custom/workspace"
branch_prefix = "ac-"
privacy = "strict"
""",
    )
    config = load_config(config_path)
    assert config.user.workspace_dir == Path("/custom/workspace")
    assert config.user.branch_prefix == "ac-"
    assert config.user.privacy == "strict"
    assert "teatree" in config.raw


def test_load_config_missing_file(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nonexistent.toml")
    assert config.user.workspace_dir == Path.home() / "workspace"
    assert config.user.branch_prefix == ""
    assert config.user.privacy == ""


def test_load_config_defaults_when_teatree_section_empty(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[other]\nfoo = 1\n")
    config = load_config(config_path)
    assert config.user.branch_prefix == ""


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


def test_agent_signature_opt_in(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nagent_signature = true\n")
    assert load_config(config_path).user.agent_signature is True


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


def test_require_human_approval_to_merge_can_be_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nrequire_human_approval_to_merge = false\n")
    assert load_config(config_path).user.require_human_approval_to_merge is False


def test_require_human_approval_to_answer_defaults_on(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.require_human_approval_to_answer is True


def test_require_human_approval_to_answer_can_be_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nrequire_human_approval_to_answer = false\n")
    assert load_config(config_path).user.require_human_approval_to_answer is False


def test_notify_on_post_on_behalf_defaults_true(tmp_path: Path) -> None:
    """#949: after-receipt visibility DM ships ON by default."""
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.notify_on_post_on_behalf is True


def test_notify_on_post_on_behalf_global_false(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nnotify_on_post_on_behalf = false\n")
    assert load_config(config_path).user.notify_on_post_on_behalf is False


def test_notify_on_post_on_behalf_no_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anti-vacuity guard: no copied-by-analogy ``T3_*`` env layer exists.

    ``notify_on_post_on_behalf`` is intentionally NOT in
    ``ENV_SETTING_OVERRIDES`` (its sibling ``notify_user_via_bot`` has no
    env peer). An env var must therefore have ZERO effect — only the
    documented dataclass-default → global → per-overlay chain resolves it.
    """
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nnotify_on_post_on_behalf = true\n")
    monkeypatch.setenv("T3_NOTIFY_ON_POST_ON_BEHALF", "false")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", config_path)
    assert get_effective_settings().notify_on_post_on_behalf is True


def test_on_behalf_post_mode_defaults_to_draft_or_ask(tmp_path: Path) -> None:
    """New default mode is DRAFT_OR_ASK — replaces the old default-true bool."""
    from teatree.config import OnBehalfPostMode  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.on_behalf_post_mode is OnBehalfPostMode.DRAFT_OR_ASK


def test_on_behalf_post_mode_explicit_immediate(tmp_path: Path) -> None:
    from teatree.config import OnBehalfPostMode  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, '[teatree]\non_behalf_post_mode = "immediate"\n')
    assert load_config(config_path).user.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE


def test_legacy_ask_before_post_on_behalf_true_maps_to_ask(tmp_path: Path) -> None:
    """Backward-compat: ``ask_before_post_on_behalf = true`` → ASK mode."""
    from teatree.config import OnBehalfPostMode  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nask_before_post_on_behalf = true\n")
    cfg = load_config(config_path)
    assert cfg.user.on_behalf_post_mode is OnBehalfPostMode.ASK
    # Derived legacy boolean stays consistent for the one deprecation release.
    assert cfg.user.ask_before_post_on_behalf is True


def test_legacy_ask_before_post_on_behalf_false_maps_to_immediate(tmp_path: Path) -> None:
    """Backward-compat: ``ask_before_post_on_behalf = false`` → IMMEDIATE mode."""
    from teatree.config import OnBehalfPostMode  # noqa: PLC0415

    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nask_before_post_on_behalf = false\n")
    cfg = load_config(config_path)
    assert cfg.user.on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE
    assert cfg.user.ask_before_post_on_behalf is False


def test_loop_cadence_seconds_defaults_to_720(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.loop_cadence_seconds == 720


def test_user_identity_aliases_defaults_empty(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.user_identity_aliases == []


def test_user_identity_aliases_reads_toml(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(
        config_path,
        '[teatree]\nuser_identity_aliases = ["adrien.work", "souliane", "adrien.cossa"]\n',
    )
    assert load_config(config_path).user.user_identity_aliases == ["adrien.work", "souliane", "adrien.cossa"]


def test_user_identity_aliases_ignores_non_list(tmp_path: Path) -> None:
    """A malformed scalar (string) is coerced to an empty list, not a crash."""
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, '[teatree]\nuser_identity_aliases = "souliane"\n')
    assert load_config(config_path).user.user_identity_aliases == []


def test_clean_ignore_defaults_empty(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.clean_ignore == []


def test_clean_ignore_reads_toml(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, '[teatree]\nclean_ignore = ["spike/*", "dev-override"]\n')
    assert load_config(config_path).user.clean_ignore == ["spike/*", "dev-override"]


def test_loop_cadence_seconds_override(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nloop_cadence_seconds = 300\n")
    assert load_config(config_path).user.loop_cadence_seconds == 300


def test_billing_cycle_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    user = load_config(config_path).user
    assert user.billing_cycle_anchor_day == 0
    assert user.sdk_monthly_credit_usd == pytest.approx(200.0)


def test_billing_cycle_anchor_and_credit_override(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nbilling_cycle_anchor_day = 15\nsdk_monthly_credit_usd = 100.0\n")
    user = load_config(config_path).user
    assert user.billing_cycle_anchor_day == 15
    assert user.sdk_monthly_credit_usd == pytest.approx(100.0)


class TestIssueImplementerSettings:
    """Config surface for the opt-in, default-OFF issue-implementer loop (#1548).

    The loop is a hard NO-OP unless ``issue_implementer_enabled`` is set;
    mirrors the ``review_skill = ""`` opt-in (#1541) and the
    ``scanning_news_*`` cadence pattern. This PR adds only the config
    knobs — the scanner/dispatch land in later PRs.
    """

    def test_defaults_are_opt_in_off(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        user = load_config(config_path).user
        assert user.issue_implementer_enabled is False
        assert user.issue_implementer_label == ""
        assert user.issue_implementer_max_concurrent == 1
        assert user.issue_implementer_cadence_hours == 1

    def test_enabled_reads_toml_bool(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nissue_implementer_enabled = true\n")
        assert load_config(config_path).user.issue_implementer_enabled is True

    def test_label_reads_toml_str(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nissue_implementer_label = "auto-implement"\n')
        assert load_config(config_path).user.issue_implementer_label == "auto-implement"

    def test_max_concurrent_reads_toml_int(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nissue_implementer_max_concurrent = 3\n")
        assert load_config(config_path).user.issue_implementer_max_concurrent == 3

    def test_cadence_hours_reads_toml_int(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nissue_implementer_cadence_hours = 6\n")
        assert load_config(config_path).user.issue_implementer_cadence_hours == 6

    def test_env_kill_switch_overrides_toml(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``T3_ISSUE_IMPLEMENTER_ENABLED`` is the operational fast-disable.

        Env wins over the toml global, so an enabled loop can be killed
        without editing ``~/.teatree.toml``.
        """
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        _write_toml(config_file, "[teatree]\nissue_implementer_enabled = true\n")
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


class TestRequireReviewContextSetting:
    """The deep-retrieval gate knob loads from toml; default is opt-in OFF."""

    def test_default_is_off(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.require_review_context is False

    def test_reads_toml_bool(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nrequire_review_context = true\n")
        assert load_config(config_path).user.require_review_context is True


class TestAutoUpdateSettings:
    """#1760: CI-green gate + deferred-reinstall flags load from toml + env."""

    def test_require_green_main_defaults_on(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.auto_update_require_green_main is True

    def test_require_green_main_reads_toml(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nauto_update_require_green_main = false\n")
        assert load_config(config_path).user.auto_update_require_green_main is False

    def test_reinstall_defaults_off(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\n")
        assert load_config(config_path).user.auto_update_reinstall is False

    def test_reinstall_reads_toml(self, tmp_path: Path) -> None:
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nauto_update_reinstall = true\n")
        assert load_config(config_path).user.auto_update_reinstall is True

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
        _write_toml(config_file, "[teatree]\nauto_update_reinstall = false\n")
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

    def test_load_config_reads_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nmode = "auto"\n')
        config = load_config(config_path)
        assert config.user.mode is Mode.AUTO

    def test_env_var_overrides_toml(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T3_MODE wins over the toml global — verified via get_effective_settings."""
        del elsewhere, no_installed_overlays
        _write_toml(config_file, '[teatree]\nmode = "auto"\n')
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

    def test_load_config_invalid_mode_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_MODE", raising=False)
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, '[teatree]\nmode = "headless"\n')

        with pytest.raises(ValueError, match="Invalid t3 mode"):
            load_config(config_path)

    def test_load_config_malformed_toml_raises_named_error(self, tmp_path: Path) -> None:
        # #1652: a TOML syntax error surfaces as a typed, message-bearing
        # config error naming the file, never a raw TOMLDecodeError traceback.
        config_path = tmp_path / ".teatree.toml"
        _write_toml(config_path, "[teatree]\nworkspace_dir = \n")

        with pytest.raises(ValueError, match="Malformed TOML") as exc_info:
            load_config(config_path)
        assert str(config_path) in str(exc_info.value)
        assert not isinstance(exc_info.value, tomllib.TOMLDecodeError)

    def test_get_mode_reflects_loaded_config(
        self,
        config_file: Path,
        elsewhere: Path,
        no_installed_overlays: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        del elsewhere, no_installed_overlays
        monkeypatch.delenv("T3_MODE", raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

        _write_toml(config_file, '[teatree]\nmode = "auto"\n')
        assert get_effective_settings().mode is Mode.AUTO

        _write_toml(config_file, '[teatree]\nmode = "interactive"\n')
        assert get_effective_settings().mode is Mode.INTERACTIVE
