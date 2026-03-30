"""Tests for teatree.core.overlay — OverlayBase default method coverage."""

from unittest.mock import MagicMock

from teatree.core.overlay import OverlayBase, ProvisionStep


class _MinimalOverlay(OverlayBase):
    """Concrete subclass implementing only the abstract methods."""

    def get_repos(self):
        return ["org/repo-a"]

    def get_provision_steps(self, worktree):
        return [ProvisionStep(name="test", callable=lambda: None)]


def _make_worktree():
    """Return a mock Worktree for passing to overlay methods."""
    return MagicMock()


def test_overlay_config_defaults():
    overlay = _MinimalOverlay()
    assert overlay.config.get_gitlab_username() == ""
    assert overlay.config.get_slack_token() == ""
    assert overlay.config.get_review_channel() == ("", "")
    assert overlay.config.get_known_variants() == []


def test_get_env_extra_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.get_env_extra(_make_worktree()) == {}


def test_get_run_commands_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.get_run_commands(_make_worktree()) == {}


def test_get_test_command_returns_empty_string():
    overlay = _MinimalOverlay()
    assert overlay.get_test_command(_make_worktree()) == ""


def test_get_db_import_strategy_returns_none():
    overlay = _MinimalOverlay()
    assert overlay.get_db_import_strategy(_make_worktree()) is None


def test_db_import_returns_false():
    overlay = _MinimalOverlay()
    assert overlay.db_import(_make_worktree()) is False


def test_db_import_with_force_returns_false():
    overlay = _MinimalOverlay()
    assert overlay.db_import(_make_worktree(), force=True) is False


def test_get_post_db_steps_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_post_db_steps(_make_worktree()) == []


def test_get_reset_passwords_command_returns_empty_string():
    overlay = _MinimalOverlay()
    assert overlay.get_reset_passwords_command(_make_worktree()) == ""


def test_get_symlinks_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_symlinks(_make_worktree()) == []


def test_get_services_config_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.get_services_config(_make_worktree()) == {}


def test_validate_mr_returns_empty_errors_and_warnings():
    overlay = _MinimalOverlay()
    result = overlay.metadata.validate_mr("title", "desc")
    assert result == {"errors": [], "warnings": []}


def test_get_followup_repos_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_followup_repos() == []


def test_get_skill_metadata_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_skill_metadata() == {}


def test_get_ci_project_path_returns_empty_string():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_ci_project_path() == ""


def test_get_e2e_config_returns_empty_dict():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_e2e_config() == {}


def test_detect_variant_returns_empty_string():
    overlay = _MinimalOverlay()
    assert overlay.metadata.detect_variant() == ""


def test_get_workspace_repos_delegates_to_get_repos():
    overlay = _MinimalOverlay()
    assert overlay.get_workspace_repos() == ["org/repo-a"]


def test_get_pre_run_steps_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.get_pre_run_steps(_make_worktree(), "frontend") == []


def test_get_tool_commands_returns_empty_list():
    overlay = _MinimalOverlay()
    assert overlay.metadata.get_tool_commands() == []


def test_abstract_methods_implemented():
    overlay = _MinimalOverlay()
    wt = _make_worktree()
    assert overlay.get_repos() == ["org/repo-a"]
    steps = overlay.get_provision_steps(wt)
    assert len(steps) == 1
    assert steps[0].name == "test"


def test_get_repos_raises_not_implemented():
    """OverlayBase.get_repos() raises NotImplementedError when called directly."""
    import pytest  # noqa: PLC0415

    with pytest.raises(NotImplementedError):
        OverlayBase.get_repos(MagicMock())


def test_get_provision_steps_raises_not_implemented():
    """OverlayBase.get_provision_steps() raises NotImplementedError when called directly."""
    import pytest  # noqa: PLC0415

    with pytest.raises(NotImplementedError):
        OverlayBase.get_provision_steps(MagicMock(), _make_worktree())
