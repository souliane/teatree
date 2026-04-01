"""Tests for the bundled t3-teatree overlay."""

from importlib.metadata import entry_points
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.contrib.t3_teatree.overlay as overlay_mod
from teatree.contrib.t3_teatree.apps import T3TeatreeConfig
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay, _repo_root
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay


class TestTeatreeOverlayIsValid:
    def test_subclasses_overlay_base(self) -> None:
        assert issubclass(TeatreeOverlay, OverlayBase)

    def test_loadable_via_overlay_loader(self) -> None:
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"t3-teatree": TeatreeOverlay()},
        ):
            overlay = get_overlay()
            assert isinstance(overlay, TeatreeOverlay)


class TestGetRepos:
    def test_returns_teatree(self) -> None:
        overlay = TeatreeOverlay()
        assert overlay.get_repos() == ["teatree"]


class TestGetWorkspaceRepos:
    def test_returns_teatree(self) -> None:
        overlay = TeatreeOverlay()
        assert overlay.get_workspace_repos() == ["teatree"]


class TestGetFollowupRepos:
    def test_returns_github_project(self) -> None:
        overlay = TeatreeOverlay()
        assert overlay.metadata.get_followup_repos() == ["souliane/teatree"]


class TestGetSkillMetadata:
    def test_returns_skill_path_and_patterns(self) -> None:
        overlay = TeatreeOverlay()
        metadata = overlay.metadata.get_skill_metadata()

        assert "skill_path" in metadata
        assert "remote_patterns" in metadata
        assert metadata["remote_patterns"] == ["souliane/teatree"]

    def test_skill_path_points_to_existing_directory(self) -> None:
        overlay = TeatreeOverlay()
        metadata = overlay.metadata.get_skill_metadata()
        skill_path = Path(str(metadata["skill_path"]))
        assert skill_path.is_dir()


class TestGetProvisionSteps(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay="t3-teatree")
        cls.worktree = Worktree.objects.create(
            ticket=cls.ticket, overlay="t3-teatree", repo_path="/tmp/teatree", branch="main"
        )

    def test_returns_sync_step(self) -> None:
        overlay = TeatreeOverlay()
        steps = overlay.get_provision_steps(self.worktree)

        assert len(steps) == 1
        assert steps[0].name == "sync-dependencies"

    def test_sync_step_runs_uv_sync(self) -> None:
        overlay = TeatreeOverlay()
        steps = overlay.get_provision_steps(self.worktree)

        with patch("subprocess.run") as mock_run:
            steps[0].callable()
            mock_run.assert_called_once_with(["uv", "sync"], cwd=Path("/tmp/teatree"), check=True)


class TestGetRunCommands(TestCase):
    def test_returns_test_and_lint(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        worktree = Worktree.objects.create(ticket=ticket, overlay="t3-teatree", repo_path="/tmp/teatree", branch="main")
        overlay = TeatreeOverlay()
        commands = overlay.get_run_commands(worktree)

        assert "test" in commands
        assert "lint" in commands
        test_cmd = commands["test"]
        assert isinstance(test_cmd, list)
        assert "pytest" in test_cmd[-1]


class TestGetTestCommand(TestCase):
    def test_returns_pytest_command(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        worktree = Worktree.objects.create(ticket=ticket, overlay="t3-teatree", repo_path="/tmp/teatree", branch="main")
        overlay = TeatreeOverlay()
        assert overlay.get_test_command(worktree) == ["uv", "run", "pytest"]


class TestRepoRoot:
    def test_finds_repo_root(self) -> None:
        root = _repo_root()
        assert (root / "pyproject.toml").is_file()
        assert (root / "skills").is_dir()

    def test_raises_when_no_markers(self, tmp_path, monkeypatch) -> None:
        """When no parent has pyproject.toml + skills/, raises FileNotFoundError."""
        fake = tmp_path / "a" / "b" / "overlay.py"
        fake.parent.mkdir(parents=True)
        fake.touch()
        monkeypatch.setattr(overlay_mod, "__file__", str(fake))
        with pytest.raises(FileNotFoundError, match="Cannot find teatree repo root"):
            overlay_mod._repo_root()


class TestEntryPointDiscovery:
    def test_registered_as_entry_point(self) -> None:
        eps = entry_points(group="teatree.overlays")
        names = [ep.name for ep in eps]
        assert "t3-teatree" in names

    def test_entry_point_resolves_to_overlay_class(self) -> None:
        eps = entry_points(group="teatree.overlays")
        ep = next(ep for ep in eps if ep.name == "t3-teatree")
        assert ep.value == "teatree.contrib.t3_teatree.overlay:TeatreeOverlay"


class TestAppsConfig:
    def test_app_name(self) -> None:
        assert T3TeatreeConfig.name == "teatree.contrib.t3_teatree"


class TestOverlayDefaults(TestCase):
    """Verify optional hooks that the teatree overlay doesn't override return defaults."""

    def test_optional_hooks_return_defaults(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        worktree = Worktree.objects.create(ticket=ticket, overlay="t3-teatree", repo_path="/tmp/teatree", branch="main")
        overlay = TeatreeOverlay()

        assert overlay.get_env_extra(worktree) == {}
        assert overlay.get_db_import_strategy(worktree) is None
        assert overlay.get_post_db_steps(worktree) == []
        assert overlay.get_symlinks(worktree) == []
        assert overlay.get_services_config(worktree) == {}
        assert overlay.metadata.validate_mr("title", "desc") == {"errors": [], "warnings": []}
        assert overlay.metadata.get_ci_project_path() == ""
        assert overlay.metadata.get_e2e_config() == {"test_dir": "e2e/", "settings_module": "e2e.settings"}
        assert overlay.metadata.detect_variant() == ""
        assert overlay.metadata.get_tool_commands() == []


class TestAsgiModule:
    def test_asgi_application_is_importable(self) -> None:
        """The ASGI entry point for teatree is importable and creates an application."""
        import importlib  # noqa: PLC0415

        mod = importlib.import_module("teatree.asgi")
        assert hasattr(mod, "application")
        assert callable(mod.application)
