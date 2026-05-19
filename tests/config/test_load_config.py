"""``load_config`` settings parsing and ``Mode`` resolution.

Split verbatim from the former monolithic ``tests/test_config.py``
(souliane/teatree#443). Covers ``load_config`` defaults and per-key
parsing (workspace dir, branch prefix, privacy, agent-signature, the
human-approval gates, loop cadence) and the ``Mode`` parser plus its
toml/env resolution via ``get_effective_settings``.

Integration-first per the Test-Writing Doctrine: real TOML fixtures
under ``tmp_path`` with ``teatree.config.CONFIG_PATH`` monkeypatched.
"""

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


def test_agent_signature_defaults_off(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\n")
    assert load_config(config_path).user.agent_signature is False


def test_agent_signature_opt_in(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nagent_signature = true\n")
    assert load_config(config_path).user.agent_signature is True


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


def test_loop_cadence_seconds_override(tmp_path: Path) -> None:
    config_path = tmp_path / ".teatree.toml"
    _write_toml(config_path, "[teatree]\nloop_cadence_seconds = 300\n")
    assert load_config(config_path).user.loop_cadence_seconds == 300


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
