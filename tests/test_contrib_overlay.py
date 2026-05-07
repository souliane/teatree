"""Tests for the bundled t3-teatree overlay."""

import subprocess
from importlib.metadata import entry_points
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import teatree.config as config_mod
import teatree.contrib.t3_teatree.overlay as overlay_mod
import teatree.core.overlay_loader as overlay_loader_mod
from teatree.contrib.t3_teatree.apps import T3TeatreeConfig
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay, _repo_root
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase
from teatree.core.overlay_loader import get_overlay


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch) -> Path:
    """Redirect overlay discovery to a fresh tmp toml, ignoring the user's real config.

    ``CONFIG_PATH`` is baked into ``load_config`` / ``discover_overlays``
    defaults at def-time, so monkeypatching the module constant alone is not
    enough — we wrap both callables to always pass the tmp path explicitly.
    """
    from functools import partial  # noqa: PLC0415

    toml_path = tmp_path / "teatree.toml"
    monkeypatch.setattr(overlay_mod, "load_config", partial(config_mod.load_config, path=toml_path))
    monkeypatch.setattr(
        overlay_mod,
        "discover_overlays",
        partial(config_mod.discover_overlays, config_path=toml_path),
    )
    return toml_path


class TestTeatreeOverlayIsValid:
    def test_subclasses_overlay_base(self) -> None:
        assert issubclass(TeatreeOverlay, OverlayBase)

    def test_loadable_via_overlay_loader(self) -> None:
        with patch.object(
            overlay_loader_mod,
            "_discover_overlays",
            return_value={"t3-teatree": TeatreeOverlay()},
        ):
            overlay = get_overlay()
            assert isinstance(overlay, TeatreeOverlay)


class TestGetRepos:
    def test_returns_teatree(self) -> None:
        overlay = TeatreeOverlay()
        assert overlay.get_repos() == ["teatree"]


class TestGetWorkspaceRepos:
    def test_falls_back_to_get_repos_when_discovery_empty(
        self, tmp_path: Path, monkeypatch, isolated_config: Path
    ) -> None:
        """Discovery empty → final fallback is ``get_repos()``."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        isolated_config.write_text(f'[teatree]\nworkspace_dir = "{workspace}"\n', encoding="utf-8")
        monkeypatch.setattr(overlay_mod, "_repo_root", lambda: tmp_path / "elsewhere")

        overlay = TeatreeOverlay()
        overlay.config.workspace_repos = []
        assert overlay.get_workspace_repos() == ["teatree"]

    def test_returns_configured_workspace_repos(self) -> None:
        overlay = TeatreeOverlay()
        overlay.config.workspace_repos = ["souliane/teatree"]
        assert overlay.get_workspace_repos() == ["souliane/teatree"]

    def test_aggregates_teatree_and_toml_overlays(self, tmp_path: Path, monkeypatch, isolated_config: Path) -> None:
        """Dynamic discovery aggregates teatree's repo + every ``[overlays.*].path``."""
        workspace = tmp_path / "workspace"
        (workspace / "souliane" / "teatree").mkdir(parents=True)
        (workspace / "acme" / "t3-acme").mkdir(parents=True)

        isolated_config.write_text(
            f'[teatree]\nworkspace_dir = "{workspace}"\n\n'
            f'[overlays.t3-acme]\npath = "{workspace / "acme" / "t3-acme"}"\n'
            'class = "t3_acme.overlay:AcmeOverlay"\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(overlay_mod, "_repo_root", lambda: workspace / "souliane" / "teatree")

        overlay = TeatreeOverlay()
        overlay.config.workspace_repos = []
        repos = overlay.get_workspace_repos()

        assert "souliane/teatree" in repos
        assert "acme/t3-acme" in repos

    def test_skips_overlays_outside_workspace_dir(self, tmp_path: Path, monkeypatch, isolated_config: Path) -> None:
        """Overlays whose path sits outside ``workspace_dir`` are silently skipped."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "elsewhere" / "rogue"
        outside.mkdir(parents=True)

        isolated_config.write_text(
            f'[teatree]\nworkspace_dir = "{workspace}"\n\n'
            f'[overlays.rogue]\npath = "{outside}"\nclass = "rogue:Overlay"\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(overlay_mod, "_repo_root", lambda: outside)

        overlay = TeatreeOverlay()
        overlay.config.workspace_repos = []
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

    def test_returns_sync_and_install_overlays_steps(self) -> None:
        overlay = TeatreeOverlay()
        steps = overlay.get_provision_steps(self.worktree)

        assert [step.name for step in steps] == ["sync-dependencies", "install-overlays-editable"]

    def test_sync_step_runs_uv_sync(self) -> None:
        overlay = TeatreeOverlay()
        steps = overlay.get_provision_steps(self.worktree)

        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mock_run:
            steps[0].callable()
            mock_run.assert_called_once()
            assert mock_run.call_args.args[0] == ["uv", "sync"]
            assert mock_run.call_args.kwargs["cwd"] == str(Path("/tmp/teatree"))


@pytest.mark.django_db
class TestInstallOverlaysEditableStep:
    """Integration tests for the install-overlays-editable provision step."""

    def _make_pyproject(self, path: Path, name: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.0.0"\n', encoding="utf-8")

    def test_installs_overlay_worktree_editable(self, tmp_path: Path, monkeypatch, isolated_config: Path) -> None:
        """Discovered overlay under workspace_dir → `uv pip install -e <overlay_worktree>` in teatree worktree."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        main_overlay = workspace / "acme" / "t3-acme"
        self._make_pyproject(main_overlay, "t3-acme")

        ticket_dir = workspace / "ac-teatree-117-ticket"
        teatree_wt = ticket_dir / "teatree"
        overlay_wt = ticket_dir / "t3-acme"
        self._make_pyproject(teatree_wt, "teatree")
        self._make_pyproject(overlay_wt, "t3-acme")

        isolated_config.write_text(
            f'[teatree]\nworkspace_dir = "{workspace}"\n\n'
            f'[overlays.t3-acme]\npath = "{main_overlay}"\nclass = "t3_acme.overlay:AcmeOverlay"\n',
            encoding="utf-8",
        )

        ticket = Ticket.objects.create(overlay="t3-teatree")
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="t3-teatree", repo_path=str(teatree_wt), branch="main"
        )

        overlay = TeatreeOverlay()
        steps = overlay.get_provision_steps(worktree)
        install_step = next(step for step in steps if step.name == "install-overlays-editable")

        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mock_run:
            install_step.callable()

        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == ["uv", "pip", "install", "-e", str(overlay_wt)]
        assert mock_run.call_args.kwargs["cwd"] == str(teatree_wt)

    def test_skips_overlays_outside_workspace_dir(self, tmp_path: Path, monkeypatch, isolated_config: Path) -> None:
        """Overlay whose main clone lives outside workspace_dir is silently skipped."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside_overlay = tmp_path / "elsewhere" / "rogue"
        self._make_pyproject(outside_overlay, "rogue")

        ticket_dir = workspace / "ac-teatree-117-ticket"
        teatree_wt = ticket_dir / "teatree"
        self._make_pyproject(teatree_wt, "teatree")
        self._make_pyproject(ticket_dir / "rogue", "rogue")

        isolated_config.write_text(
            f'[teatree]\nworkspace_dir = "{workspace}"\n\n'
            f'[overlays.rogue]\npath = "{outside_overlay}"\nclass = "rogue:Overlay"\n',
            encoding="utf-8",
        )

        ticket = Ticket.objects.create(overlay="t3-teatree")
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="t3-teatree", repo_path=str(teatree_wt), branch="main"
        )

        overlay = TeatreeOverlay()
        steps = overlay.get_provision_steps(worktree)
        install_step = next(step for step in steps if step.name == "install-overlays-editable")

        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mock_run:
            install_step.callable()

        mock_run.assert_not_called()

    def test_skips_self_when_teatree_overlay_is_discovered(
        self, tmp_path: Path, monkeypatch, isolated_config: Path
    ) -> None:
        """The teatree entry-point overlay resolves to the teatree worktree — skip to avoid redundant re-install."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        ticket_dir = workspace / "ac-teatree-117-ticket"
        teatree_wt = ticket_dir / "teatree"
        self._make_pyproject(teatree_wt, "teatree")

        isolated_config.write_text(
            f'[teatree]\nworkspace_dir = "{workspace}"\n\n'
            f'[overlays.t3-teatree]\npath = "{teatree_wt}"\n'
            'class = "teatree.contrib.t3_teatree.overlay:TeatreeOverlay"\n',
            encoding="utf-8",
        )

        ticket = Ticket.objects.create(overlay="t3-teatree")
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="t3-teatree", repo_path=str(teatree_wt), branch="main"
        )

        overlay = TeatreeOverlay()
        steps = overlay.get_provision_steps(worktree)
        install_step = next(step for step in steps if step.name == "install-overlays-editable")

        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mock_run:
            install_step.callable()

        mock_run.assert_not_called()

    def test_skips_overlays_without_worktree(self, tmp_path: Path, monkeypatch, isolated_config: Path) -> None:
        """Overlay with main clone under workspace_dir but no sibling worktree is silently skipped."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        main_overlay = workspace / "acme" / "t3-acme"
        self._make_pyproject(main_overlay, "t3-acme")

        ticket_dir = workspace / "ac-teatree-117-ticket"
        teatree_wt = ticket_dir / "teatree"
        self._make_pyproject(teatree_wt, "teatree")

        isolated_config.write_text(
            f'[teatree]\nworkspace_dir = "{workspace}"\n\n'
            f'[overlays.t3-acme]\npath = "{main_overlay}"\nclass = "t3_acme.overlay:AcmeOverlay"\n',
            encoding="utf-8",
        )

        ticket = Ticket.objects.create(overlay="t3-teatree")
        worktree = Worktree.objects.create(
            ticket=ticket, overlay="t3-teatree", repo_path=str(teatree_wt), branch="main"
        )

        overlay = TeatreeOverlay()
        steps = overlay.get_provision_steps(worktree)
        install_step = next(step for step in steps if step.name == "install-overlays-editable")

        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as mock_run:
            install_step.callable()

        mock_run.assert_not_called()


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
        assert overlay.metadata.get_e2e_config() == {}
        assert overlay.metadata.detect_variant() == ""
        assert overlay.metadata.get_tool_commands() == []
