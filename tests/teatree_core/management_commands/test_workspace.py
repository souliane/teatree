"""Tests for the workspace and worktree management commands."""

import os
import subprocess
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils.module_loading import import_string

import teatree.core.cleanup as cleanup_mod
import teatree.core.management.commands._workspace_cleanup as ws_cleanup_mod
import teatree.core.management.commands.workspace as workspace_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.runners.provision as provision_mod
import teatree.utils.db as db_mod
import teatree.utils.git as git_mod
import teatree.utils.run as utils_run_mod
from teatree.core.management.commands.workspace import _branch_prefix, _workspace_dir
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep
from tests.teatree_core.management_commands._overlays import (
    FULL_OVERLAY,
    NESTED_OVERLAY,
    SETTINGS,
    FullOverlay,
    _patch_overlays,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


# ── Workspace helpers ──────────────────────────────────────────────


class TestBranchPrefix(TestCase):
    def test_from_env(self) -> None:
        with patch.dict("os.environ", {"T3_BRANCH_PREFIX": "xy"}):
            assert _branch_prefix() == "xy"

    def test_from_git_config(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=False),
            patch.object(workspace_mod.git, "run", return_value="Ada Lovelace"),
        ):
            os.environ.pop("T3_BRANCH_PREFIX", None)
            assert _branch_prefix() == "al"

    def test_fallback_to_dev(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=False),
            patch.object(workspace_mod.git, "run", return_value=""),
        ):
            os.environ.pop("T3_BRANCH_PREFIX", None)
            assert _branch_prefix() == "dev"


class TestWorkspaceDirHelper(TestCase):
    def test_uses_config_workspace_dir(self) -> None:
        from teatree.config import TeaTreeConfig, UserSettings  # noqa: PLC0415

        cfg = TeaTreeConfig(user=UserSettings(workspace_dir=Path("/tmp/ws-test")))
        with patch.object(workspace_mod, "load_config", return_value=cfg):
            result = _workspace_dir()
            assert result == Path("/tmp/ws-test")


# ── Workspace commands ──────────────────────────────────────────────


class TestWorkspaceTicket(TestCase):
    def setUp(self) -> None:
        super().setUp()
        mock_result = MagicMock(returncode=0, stdout="dev", stderr="")
        self.enterContext(
            patch.object(utils_run_mod.subprocess, "run", return_value=mock_result),
        )
        # Seed the host-style clones the provisioner expects under HOME/workspace.
        # Tests that override T3_WORKSPACE_DIR ignore these and create their own.
        workspace = Path(os.environ["HOME"]) / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        for repo in ("backend", "frontend", "api", "web"):
            (workspace / repo / ".git").mkdir(parents=True, exist_ok=True)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_creates_ticket_and_worktrees(self) -> None:
        ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/42"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.issue_url == "https://example.com/issues/42"
        # Stage 3 of #140: workspace ticket advances scope() then start() so the
        # provisioning runner can materialise the worktrees in the same call.
        assert ticket.state == Ticket.State.STARTED
        assert ticket.repos == ["backend", "frontend"]
        assert ticket.worktrees.count() == 2

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_variant(self) -> None:
        ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/43", variant="acme"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.variant == "acme"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_custom_repos(self) -> None:
        ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/92", repos="api,web"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.repos == ["api", "web"]
        assert ticket.worktrees.count() == 2

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_with_git_worktree_creation(self) -> None:
        """Test the workspace ticket command with successful git worktree creation."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir()

            # Create git repos
            for repo_name in ("backend", "frontend"):
                repo_dir = workspace / repo_name
                repo_dir.mkdir()
                (repo_dir / ".git").mkdir()
                (repo_dir / ".python-version").write_text("3.12.6")

            mock_result = MagicMock()
            mock_result.returncode = 0

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", return_value=mock_result),
            ):
                ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/80"))

            ticket = Ticket.objects.get(pk=ticket_id)
            assert ticket.worktrees.count() == 2

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_description(self) -> None:
        ticket_id = cast(
            "int",
            call_command("workspace", "ticket", "https://example.com/issues/99", description="Add Login Page"),
        )
        ticket = Ticket.objects.get(pk=ticket_id)
        worktree = ticket.worktrees.first()
        assert worktree.branch.endswith("-add-login-page")
        assert "ticket" not in worktree.branch

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_warns_about_existing_orphans_before_creating(self) -> None:
        """Creating a new ticket surfaces orphan branches already in the workspace."""
        from io import StringIO  # noqa: PLC0415

        from teatree.core.orphan_guard import BranchReport, BranchStatus  # noqa: PLC0415

        fake_orphans = [
            BranchReport(repo="/ws/org/repo", branch="feat-old", status=BranchStatus.PUSHED_ORPHAN, ahead_count=3),
        ]
        stderr_buf = StringIO()
        with patch(
            "teatree.core.management.commands.workspace.find_orphans_in_workspace",
            return_value=fake_orphans,
        ):
            call_command("workspace", "ticket", "https://example.com/issues/500", stderr=stderr_buf)

        written = stderr_buf.getvalue()
        assert "orphan branch" in written.lower()
        assert "feat-old" in written

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_auto_derives_slug_from_issue_title(self) -> None:
        """When no description given, uses overlay.get_issue_title to derive slug."""
        overlay = import_string(FULL_OVERLAY)()
        overlay.get_issue_title = lambda url: "Fix Login Flow"

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": overlay}):
            ticket_id = cast(
                "int",
                call_command("workspace", "ticket", "https://example.com/issues/130"),
            )

        ticket = Ticket.objects.get(pk=ticket_id)
        worktree = ticket.worktrees.first()
        assert worktree.branch.endswith("-fix-login-flow")
        assert "ticket" not in worktree.branch

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_falls_back_to_ticket_when_title_fetch_fails(self) -> None:
        """When get_issue_title returns empty, falls back to 'ticket' slug."""
        overlay = import_string(FULL_OVERLAY)()
        overlay.get_issue_title = lambda url: ""

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": overlay}):
            ticket_id = cast(
                "int",
                call_command("workspace", "ticket", "https://example.com/issues/131"),
            )

        ticket = Ticket.objects.get(pk=ticket_id)
        worktree = ticket.worktrees.first()
        assert worktree.branch.endswith("-ticket")

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_partial_failure_when_one_repo_has_no_clone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir()
            (workspace / "backend" / ".git").mkdir(parents=True)
            (workspace / "frontend").mkdir()

            mock_result = MagicMock()
            mock_result.returncode = 0

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", return_value=mock_result),
            ):
                ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/81"))

            ticket = Ticket.objects.get(pk=ticket_id)
            assert ticket.worktrees.count() == 1
            assert ticket.worktrees.get().repo_path == "backend"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_handles_worktree_already_exists(self) -> None:
        """When worktree path already exists, it's skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir()
            (workspace / "backend").mkdir()
            (workspace / "backend" / ".git").mkdir()
            (workspace / "frontend").mkdir()
            (workspace / "frontend" / ".git").mkdir()

            # Pre-create the ticket_dir/backend to simulate existing worktree
            prefix = "ac"
            branch = f"{prefix}-backend-82-ticket"
            ticket_dir = workspace / branch
            ticket_dir.mkdir(parents=True)
            (ticket_dir / "backend").mkdir()

            mock_result = MagicMock()
            mock_result.returncode = 0

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(workspace_mod, "_branch_prefix", return_value="ac"),
                patch.object(utils_run_mod.subprocess, "run", return_value=mock_result),
            ):
                ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/82"))

            ticket = Ticket.objects.get(pk=ticket_id)
            assert ticket.worktrees.count() == 2

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_rolls_back_when_all_worktrees_fail(self) -> None:
        """When all git worktree add fail, ticket and DB entries are rolled back."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir()
            (workspace / "backend").mkdir()
            (workspace / "backend" / ".git").mkdir()
            (workspace / "frontend").mkdir()
            (workspace / "frontend" / ".git").mkdir()

            mock_pull = MagicMock()
            mock_pull.returncode = 0

            mock_add = MagicMock()
            mock_add.returncode = 1
            mock_add.stderr = "fatal: branch already exists"

            def side_effect(cmd, **kwargs):
                if "worktree" in cmd:
                    return mock_add
                return mock_pull

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", side_effect=side_effect),
            ):
                result = cast("int", call_command("workspace", "ticket", "https://example.com/issues/83"))

            assert result == 0
            assert Ticket.objects.filter(issue_url="https://example.com/issues/83").count() == 0
            assert Worktree.objects.count() == 0

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_reruns_without_duplicates(self) -> None:
        """Running ticket twice with the same issue_url does not duplicate Worktree entries."""
        call_command("workspace", "ticket", "https://example.com/issues/100")
        first_count = Worktree.objects.filter(ticket__issue_url="https://example.com/issues/100").count()

        call_command("workspace", "ticket", "https://example.com/issues/100")
        second_count = Worktree.objects.filter(ticket__issue_url="https://example.com/issues/100").count()

        assert first_count == second_count
        assert Ticket.objects.filter(issue_url="https://example.com/issues/100").count() == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_adds_missing_repo_to_existing_ticket(self) -> None:
        """Re-running ticket with additional repos adds them without duplicating existing ones."""
        ticket_id = cast(
            "int",
            call_command("workspace", "ticket", "https://example.com/issues/101", repos="backend"),
        )
        assert Worktree.objects.filter(ticket_id=ticket_id).count() == 1

        call_command("workspace", "ticket", "https://example.com/issues/101", repos="backend,frontend")
        assert Worktree.objects.filter(ticket_id=ticket_id).count() == 2
        assert Worktree.objects.filter(ticket_id=ticket_id, repo_path="frontend").exists()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_recovers_from_branch_already_exists(self) -> None:
        """When 'git worktree add -b' fails with 'already exists', retry without -b."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir()
            (workspace / "backend").mkdir()
            (workspace / "backend" / ".git").mkdir()

            mock_pull = MagicMock(returncode=0)
            mock_add_b_fail = MagicMock(returncode=1, stderr="fatal: a branch named 'x' already exists")
            mock_add_ok = MagicMock(returncode=0)

            call_count = {"worktree": 0}

            def side_effect(cmd, **kwargs):
                if "worktree" in cmd:
                    call_count["worktree"] += 1
                    if call_count["worktree"] == 1:
                        return mock_add_b_fail  # first try with -b fails
                    return mock_add_ok  # retry without -b succeeds
                return mock_pull

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", side_effect=side_effect),
            ):
                ticket_id = cast(
                    "int",
                    call_command("workspace", "ticket", "https://example.com/issues/102", repos="backend"),
                )

            assert ticket_id > 0
            wt = Worktree.objects.get(ticket_id=ticket_id)
            assert wt.extra.get("worktree_path")

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_cleans_up_failed_worktrees_on_partial_failure(self) -> None:
        """When some repos fail, their Worktree DB entries are deleted but successful ones remain."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir()
            (workspace / "backend").mkdir()
            (workspace / "backend" / ".git").mkdir()
            (workspace / "frontend").mkdir()
            (workspace / "frontend" / ".git").mkdir()

            mock_pull = MagicMock(returncode=0)
            mock_add_ok = MagicMock(returncode=0)
            mock_add_fail = MagicMock(returncode=1, stderr="fatal: some error")

            def side_effect(cmd, **kwargs):
                if "worktree" not in cmd:
                    return mock_pull
                # backend succeeds, frontend fails
                # Check -C argument for repo path (git.worktree_add uses -C instead of cwd)
                repo = ""
                if "-C" in cmd:
                    repo = cmd[cmd.index("-C") + 1]
                else:
                    cwd = kwargs.get("cwd", Path())
                    repo = str(cwd)
                if "frontend" in repo:
                    return mock_add_fail
                return mock_add_ok

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", side_effect=side_effect),
            ):
                ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/103"))

            assert ticket_id > 0
            # Backend worktree should exist, frontend should have been cleaned up
            assert Worktree.objects.filter(ticket_id=ticket_id, repo_path="backend").exists()
            assert not Worktree.objects.filter(ticket_id=ticket_id, repo_path="frontend").exists()

    @_patch_overlays(NESTED_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-test")
    def test_nested_repo_paths(self) -> None:
        """Repos in nested subdirectories (e.g. org/backend) are found and worktrees use basenames."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            (workspace / "org" / "backend").mkdir(parents=True)
            (workspace / "org" / "backend" / ".git").mkdir()
            (workspace / "org" / "frontend").mkdir(parents=True)
            (workspace / "org" / "frontend" / ".git").mkdir()

            mock_result = MagicMock()
            mock_result.returncode = 0

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", return_value=mock_result),
            ):
                ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/90"))

            ticket = Ticket.objects.get(pk=ticket_id)
            assert ticket.worktrees.count() == 2

            repo_paths = sorted(ticket.worktrees.values_list("repo_path", flat=True))
            assert repo_paths == ["org/backend", "org/frontend"]

            branch = ticket.worktrees.first().branch
            assert "/" not in branch.split("-")[1]
            assert "backend" in branch

    @_patch_overlays(NESTED_OVERLAY)
    @override_settings(**SETTINGS)
    def test_config_workspace_repos_overrides_get_repos(self) -> None:
        """get_workspace_repos() returns config.workspace_repos when set."""
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        overlay = get_overlay()
        assert overlay.get_workspace_repos() == ["org/backend", "org/frontend"]
        assert overlay.get_repos() == ["backend", "frontend"]


_no_prune = patch.object(workspace_mod, "prune_branches", new=lambda _repo: [])


_no_stash = patch.object(workspace_mod, "drop_orphaned_stashes", new=lambda _repo: [])


_no_orphan_dbs = patch.object(workspace_mod, "drop_orphan_databases", new=list)


_no_dslr_prune = patch("teatree.utils.django_db.prune_dslr_snapshots", new=lambda **kw: [])


class TestWorkspaceCleanAll(TestCase):
    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_removes_stale_worktrees(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/50")
        Worktree.objects.create(overlay="test", ticket=ticket, repo_path="/tmp/test", branch="feature")

        cleaned = cast("list[str]", call_command("workspace", "clean-all"))

        assert len(cleaned) == 1
        assert Worktree.objects.count() == 0

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_removes_git_worktree_and_branch(self) -> None:
        """clean-all delegates to cleanup_worktree which calls git worktree remove + branch -D."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            repo_main = workspace / "backend"
            repo_main.mkdir(parents=True)
            (repo_main / ".git").mkdir()

            wt_dir = workspace / "ac-backend-80-ticket" / "backend"
            wt_dir.mkdir(parents=True)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/80")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="ac-backend-80-ticket",
                extra={"worktree_path": str(wt_dir)},
            )

            mock_config = MagicMock()
            mock_config.user.workspace_dir = workspace
            with (
                patch.object(cleanup_mod, "load_config", return_value=mock_config),
                patch.object(cleanup_mod, "git") as mock_git,
                patch.object(cleanup_mod, "get_overlay") as mock_overlay,
            ):
                mock_overlay.return_value.get_cleanup_steps.return_value = []
                mock_git.status_porcelain.return_value = ""
                mock_git.unsynced_commits.return_value = []
                mock_git.commits_absent_from_all_remotes.return_value = []
                cleaned = cast("list[str]", call_command("workspace", "clean-all"))

            assert any("Cleaned: backend" in c for c in cleaned)
            assert Worktree.objects.count() == 0

            mock_git.worktree_remove.assert_called_once()
            mock_git.branch_delete.assert_called_once_with(str(repo_main), "ac-backend-80-ticket")

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_drops_database_when_db_name_set(self) -> None:
        """clean-all calls dropdb when worktree has a db_name."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir(parents=True)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/81")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="ac-backend-81-ticket",
                extra={},
            )
            # Set db_name directly (bypass FSM provision)
            Worktree.objects.filter(pk=wt.pk).update(db_name="wt_test_db")

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(
                    utils_run_mod.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "", ""),
                ) as mock_run,
                patch.object(db_mod, "pg_host", return_value="localhost"),
                patch.object(db_mod, "pg_user", return_value="testuser"),
                patch.object(db_mod, "pg_env", return_value={"PGPASSWORD": "secret"}),
            ):
                cleaned = cast("list[str]", call_command("workspace", "clean-all"))

            assert len(cleaned) == 1
            assert Worktree.objects.count() == 0

            # Should have called dropdb
            assert mock_run.call_count == 1
            dropdb_call = mock_run.call_args_list[0]
            assert "dropdb" in dropdb_call[0][0]
            assert "wt_test_db" in dropdb_call[0][0]

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_warns_on_uncommitted_changes(self) -> None:
        """clean-all warns when a worktree directory has uncommitted changes."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir(parents=True)

            wt_dir = workspace / "ac-backend-85-ticket" / "backend"
            wt_dir.mkdir(parents=True)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/85")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="ac-backend-85-ticket",
                extra={"worktree_path": str(wt_dir)},
            )

            mock_config = MagicMock()
            mock_config.user.workspace_dir = workspace
            with (
                patch.object(cleanup_mod, "load_config", return_value=mock_config),
                patch.object(cleanup_mod, "git") as mock_git,
                patch.object(cleanup_mod, "get_overlay") as mock_overlay,
                self.assertLogs("teatree.core.cleanup", level="WARNING") as logs,
            ):
                mock_overlay.return_value.get_cleanup_steps.return_value = []
                mock_git.status_porcelain.return_value = " M dirty_file.py"
                mock_git.commits_absent_from_all_remotes.return_value = []
                call_command("workspace", "clean-all")

            assert any("uncommitted changes" in msg for msg in logs.output)

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_overlay_cleanup_steps(self) -> None:
        """clean-all invokes overlay.get_cleanup_steps() for each worktree."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir(parents=True)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/86")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="ac-backend-86-ticket",
                extra={},
            )

            cleanup_called = []

            class CleanupOverlay(FullOverlay):
                def get_cleanup_steps(self, worktree: Worktree) -> list[ProvisionStep]:
                    return [ProvisionStep(name="docker-down", callable=lambda: cleanup_called.append(True))]

            cleanup_overlay = CleanupOverlay()
            result: dict[str, OverlayBase] = {"test": cleanup_overlay}

            def _fake_discover() -> dict[str, OverlayBase]:
                return result

            _fake_discover.cache_clear = lambda: None

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover),
            ):
                call_command("workspace", "clean-all")

            assert cleanup_called == [True]

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_removes_empty_ticket_directories(self) -> None:
        """clean-all removes empty directories in workspace after cleaning worktrees."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            workspace.mkdir(parents=True)

            # Create an empty directory that should be cleaned up
            empty_dir = workspace / "ac-backend-90-ticket"
            empty_dir.mkdir()

            # Create a non-empty directory that should NOT be removed
            nonempty_dir = workspace / "ac-backend-91-ticket"
            nonempty_dir.mkdir()
            (nonempty_dir / "some_file.txt").write_text("content", encoding="utf-8")

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
            ):
                cleaned = cast("list[str]", call_command("workspace", "clean-all"))

            # Only the empty dir should be removed
            assert any("ac-backend-90-ticket" in c for c in cleaned)
            assert not any("ac-backend-91-ticket" in c for c in cleaned)
            assert not empty_dir.exists()
            assert nonempty_dir.exists()

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_includes_dslr_snapshot_pruning(self) -> None:
        """clean-all includes DSLR snapshot pruning results."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(workspace_mod, "_workspace_dir", return_value=Path(tmp)),
            patch.object(provision_mod, "_workspace_dir", return_value=Path(tmp)),
            patch("teatree.utils.django_db.prune_dslr_snapshots", return_value=["old-snapshot-2025"]),
        ):
            cleaned = cast("list[str]", call_command("workspace", "clean-all"))

        assert any("old-snapshot-2025" in c for c in cleaned)

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_continues_when_one_worktree_refuses_cleanup(self) -> None:
        """clean-all skips worktrees with unsynced commits and still cleans the rest."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            for repo in ("frontend", "backend"):
                repo_main = workspace / repo
                repo_main.mkdir(parents=True)
                (repo_main / ".git").mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/360")
            stuck_wt_dir = workspace / "ac-frontend-360-ticket" / "frontend"
            stuck_wt_dir.mkdir(parents=True)
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="frontend",
                branch="ac-frontend-360-ticket",
                extra={"worktree_path": str(stuck_wt_dir)},
            )
            clean_wt_dir = workspace / "ac-backend-360-ticket" / "backend"
            clean_wt_dir.mkdir(parents=True)
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="ac-backend-360-ticket",
                extra={"worktree_path": str(clean_wt_dir)},
            )

            def _unsynced(_repo: str, branch: str) -> list[str]:
                return ["abc123 chore: unpushed"] if branch == "ac-frontend-360-ticket" else []

            def _classify(_repo: str, branch: str, target: str = "origin/main") -> cleanup_mod.BranchClassification:
                if branch == "ac-frontend-360-ticket":
                    return cleanup_mod.BranchClassification(
                        genuinely_ahead=[
                            cleanup_mod.BranchCommit(sha="abc123", subject="chore: unpushed", is_merge=False),
                        ],
                    )
                return cleanup_mod.BranchClassification()

            mock_config = MagicMock()
            mock_config.user.workspace_dir = workspace
            with (
                patch.object(cleanup_mod, "load_config", return_value=mock_config),
                patch.object(cleanup_mod, "git") as mock_git,
                patch.object(cleanup_mod, "get_overlay") as mock_overlay,
                patch.object(cleanup_mod, "classify_branch_commits", side_effect=_classify),
            ):
                mock_overlay.return_value.get_cleanup_steps.return_value = []
                mock_git.status_porcelain.return_value = ""
                mock_git.unsynced_commits.side_effect = _unsynced
                # Both branches are pushed to their own remote ref, so the
                # #706 data-loss guard passes; this test targets the stricter
                # origin/main hygiene refusal on the frontend branch.
                mock_git.commits_absent_from_all_remotes.return_value = []
                cleaned = cast("list[str]", call_command("workspace", "clean-all"))

            assert any("Cleaned: backend" in c for c in cleaned)
            assert any("ac-frontend-360-ticket" in c and "unsynced" in c.lower() for c in cleaned)
            assert Worktree.objects.filter(branch="ac-backend-360-ticket").count() == 0
            assert Worktree.objects.filter(branch="ac-frontend-360-ticket").count() == 1


class TestResolveUnsyncedWorktree(TestCase):
    """Interactive push/abandon/skip resolution for worktrees with unpushed work."""

    def _make_worktree(self, wt_path: str = "/tmp/wt") -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/379")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch="ac-backend-379-ticket",
            extra={"worktree_path": wt_path},
        )

    def test_non_tty_preserves_skip_behaviour(self) -> None:
        wt = self._make_worktree()
        exc = RuntimeError("2 unsynced commit(s) not on origin/main: foo")
        result = ws_cleanup_mod.resolve_unsynced_worktree(wt, exc, interactive=False)
        assert result.startswith("Skipped:")
        assert "unsynced" in result

    def test_interactive_default_is_skip(self) -> None:
        wt = self._make_worktree()
        exc = RuntimeError("1 unsynced commit(s) not on origin/main: bar")
        with patch("builtins.input", return_value=""):
            result = ws_cleanup_mod.resolve_unsynced_worktree(wt, exc, interactive=True)
        assert result.startswith("Skipped:")

    def test_interactive_eof_falls_back_to_skip(self) -> None:
        wt = self._make_worktree()
        exc = RuntimeError("whatever")
        with patch("builtins.input", side_effect=EOFError):
            result = ws_cleanup_mod.resolve_unsynced_worktree(wt, exc, interactive=True)
        assert result.startswith("Skipped:")

    def test_interactive_push_success_suggests_pr_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._make_worktree(wt_path=tmp)
            exc = RuntimeError("work pending")
            fake_push = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with (
                patch("builtins.input", return_value="p"),
                patch.object(utils_run_mod.subprocess, "run", return_value=fake_push) as mock_run,
            ):
                result = ws_cleanup_mod.resolve_unsynced_worktree(wt, exc, interactive=True)
        assert result.startswith("Pushed:")
        assert "pr create" in result
        args = mock_run.call_args[0][0]
        assert args[:2] == ["git", "-C"]
        assert args[-3:] == ["push", "-u", "origin"] or args[-4:-1] == ["push", "-u", "origin"]

    def test_interactive_push_failure_reports_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = self._make_worktree(wt_path=tmp)
            exc = RuntimeError("work pending")
            fake_push = subprocess.CompletedProcess([], 1, stdout="", stderr="remote rejected: protected branch")
            with (
                patch("builtins.input", return_value="p"),
                patch.object(utils_run_mod.subprocess, "run", return_value=fake_push),
            ):
                result = ws_cleanup_mod.resolve_unsynced_worktree(wt, exc, interactive=True)
        assert result.startswith("Push failed:")
        assert "protected branch" in result

    def test_interactive_push_missing_worktree_path(self) -> None:
        wt = self._make_worktree(wt_path="/tmp/does-not-exist-12345")
        exc = RuntimeError("pending")
        with patch("builtins.input", return_value="p"):
            result = ws_cleanup_mod.resolve_unsynced_worktree(wt, exc, interactive=True)
        assert result.startswith("Push failed:")
        assert "worktree path missing" in result

    def test_interactive_abandon_force_cleans(self) -> None:
        wt = self._make_worktree()
        exc = RuntimeError("pending")
        with (
            patch("builtins.input", return_value="a"),
            patch.object(ws_cleanup_mod, "cleanup_worktree", return_value="Cleaned: backend (branch)") as mock_clean,
        ):
            result = ws_cleanup_mod.resolve_unsynced_worktree(wt, exc, interactive=True)
        assert result == "Cleaned: backend (branch)"
        mock_clean.assert_called_once_with(wt, force=True)

    def test_interactive_abandon_failure_reports_error(self) -> None:
        wt = self._make_worktree()
        exc = RuntimeError("pending")
        with (
            patch("builtins.input", return_value="a"),
            patch.object(ws_cleanup_mod, "cleanup_worktree", side_effect=OSError("boom")),
        ):
            result = ws_cleanup_mod.resolve_unsynced_worktree(wt, exc, interactive=True)
        assert result.startswith("Abandon failed:")
        assert "boom" in result


class TestWorkspaceCleanMerged(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_merged_tickets_returns_placeholder(self) -> None:
        cleaned = cast("list[str]", call_command("workspace", "clean-merged"))
        assert cleaned == ["No merged tickets have lingering worktrees."]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_cleans_worktrees_of_merged_tickets(self) -> None:
        merged = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/70",
            state=Ticket.State.MERGED,
        )
        Worktree.objects.create(overlay="test", ticket=merged, repo_path="repo", branch="ac-repo-70")
        other = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/71")
        Worktree.objects.create(overlay="test", ticket=other, repo_path="repo2", branch="ac-repo2-71")

        with patch.object(workspace_mod, "cleanup_worktree", return_value="Cleaned: repo (ac-repo-70)") as mock_cleanup:
            cleaned = cast("list[str]", call_command("workspace", "clean-merged"))

        assert cleaned == ["Cleaned: repo (ac-repo-70)"]
        assert mock_cleanup.call_count == 1
        # #706 — clean-merged must NOT force-bypass the data-loss guard. The
        # ticket is MERGED so the origin/main hygiene gate is skipped, but
        # commits absent from every remote still block teardown.
        assert mock_cleanup.call_args.kwargs.get("force") is not True
        assert mock_cleanup.call_args.kwargs.get("strict_hygiene") is False

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_surfaces_cleanup_failures(self) -> None:
        merged = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/72",
            state=Ticket.State.MERGED,
        )
        Worktree.objects.create(overlay="test", ticket=merged, repo_path="repo", branch="ac-repo-72")

        with patch.object(workspace_mod, "cleanup_worktree", side_effect=RuntimeError("docker down failed")):
            cleaned = cast("list[str]", call_command("workspace", "clean-merged"))

        assert any("FAILED" in c and "docker down failed" in c for c in cleaned)


def _subprocess_side_effect(gh_stdout: str, glab_stdout: str):
    """Return a side_effect function that dispatches mock stdout based on the CLI command."""

    def _side_effect(args, **kwargs):
        cmd = args[0] if args else ""
        stdout = gh_stdout if cmd == "gh" else glab_stdout
        return subprocess.CompletedProcess([], 0, stdout=stdout)

    return _side_effect


_gh_no_pr = patch(
    "teatree.utils.run.subprocess.run",
    side_effect=_subprocess_side_effect(gh_stdout="[]", glab_stdout=""),
)


_gh_merged_pr = patch(
    "teatree.utils.run.subprocess.run",
    return_value=subprocess.CompletedProcess([], 0, stdout='[{"number":1}]'),
)


_glab_merged_mr = patch(
    "teatree.utils.run.subprocess.run",
    side_effect=_subprocess_side_effect(gh_stdout="[]", glab_stdout="!5\tMR title\t(feature)\t1 hour ago"),
)


class TestPruneBranches(TestCase):
    def test_squash_merged_detected_via_gh_api(self) -> None:
        with _gh_merged_pr:
            assert ws_cleanup_mod.is_squash_merged("/repo", "feature", "main") is True

    def test_squash_merged_detected_via_glab_api(self) -> None:
        with _glab_merged_mr:
            assert ws_cleanup_mod.is_squash_merged("/repo", "feature", "main") is True

    def test_squash_merged_fallback_via_empty_diff(self) -> None:
        with _gh_no_pr, patch.object(git_mod, "run", return_value=""):
            assert ws_cleanup_mod.is_squash_merged("/repo", "feature", "main") is True

    def test_non_squash_merged_detected_via_nonempty_diff(self) -> None:
        with _gh_no_pr, patch.object(git_mod, "run", return_value=" file.py | 1 +"):
            assert ws_cleanup_mod.is_squash_merged("/repo", "feature", "main") is False

    def test_worktree_map_parses_porcelain(self) -> None:
        porcelain = (
            "worktree /home/user/main\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /home/user/wt-feature\n"
            "HEAD def456\n"
            "branch refs/heads/feature-branch\n"
        )
        with patch.object(git_mod, "run", return_value=porcelain):
            result = ws_cleanup_mod.worktree_map("/repo")
        assert result == {"main": "/home/user/main", "feature-branch": "/home/user/wt-feature"}

    @_no_stash
    @_no_orphan_dbs
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_prune_removes_squash_merged_worktree_branch(self) -> None:
        wt_map = {"gone-branch": "/tmp/old-worktree"}
        gone_output = "  gone-branch abc123 [gone] some msg"
        merged_output = ""

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return gone_output
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return merged_output
            if args == ["branch", "--no-color"]:
                return "* main\n  gone-branch"
            if args == ["worktree", "list", "--porcelain"]:
                return "worktree /tmp/old-worktree\nHEAD abc123\nbranch refs/heads/gone-branch\n"
            return ""

        gh_merged = subprocess.CompletedProcess([], 0, stdout='[{"number":1}]')
        with (
            patch.object(workspace_mod, "_workspace_dir", return_value=Path("/tmp/ws")),
            patch.object(provision_mod, "_workspace_dir", return_value=Path("/tmp/ws")),
            patch.object(ws_cleanup_mod, "worktree_map", return_value=wt_map),
            patch.object(ws_cleanup_mod, "worktree_branches", return_value={"gone-branch"}),
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "worktree_remove", return_value=True) as mock_wt_rm,
            patch.object(git_mod, "branch_delete", return_value=True) as mock_br_del,
            patch("teatree.utils.run.subprocess.run", return_value=gh_merged),
        ):
            cleaned = ws_cleanup_mod.prune_branches("/repo")

        assert any("squash-merged" in c for c in cleaned)
        mock_wt_rm.assert_called_once_with("/repo", "/tmp/old-worktree")
        mock_br_del.assert_called_once_with("/repo", "gone-branch")

    def test_pass3_blocks_squash_merged_branch_with_unsynced_commits(self) -> None:
        wt_map = {"gone-branch": "/tmp/old-worktree"}

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return "  gone-branch abc123 [gone] some msg"
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return ""
            if args == ["branch", "--no-color"]:
                return "* main\n  gone-branch"
            if args == ["worktree", "list", "--porcelain"]:
                return "worktree /tmp/old-worktree\nHEAD abc123\nbranch refs/heads/gone-branch\n"
            return ""

        gh_merged = subprocess.CompletedProcess([], 0, stdout='[{"number":1}]')
        with (
            patch.object(workspace_mod, "_workspace_dir", return_value=Path("/tmp/ws")),
            patch.object(provision_mod, "_workspace_dir", return_value=Path("/tmp/ws")),
            patch.object(ws_cleanup_mod, "worktree_map", return_value=wt_map),
            patch.object(ws_cleanup_mod, "worktree_branches", return_value={"gone-branch"}),
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "unsynced_commits", return_value=["abc123 chore: cve fix"]),
            patch.object(git_mod, "worktree_remove", return_value=True) as mock_wt_rm,
            patch.object(git_mod, "branch_delete", return_value=True) as mock_br_del,
            patch("teatree.utils.run.subprocess.run", return_value=gh_merged),
        ):
            cleaned = ws_cleanup_mod.prune_branches("/repo")

        assert any("SKIPPED" in c and "gone-branch" in c for c in cleaned)
        mock_wt_rm.assert_not_called()
        mock_br_del.assert_not_called()

    def test_pass3_proceeds_normally_when_fully_synced(self) -> None:
        wt_map = {"gone-branch": "/tmp/old-worktree"}

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return "  gone-branch abc123 [gone] some msg"
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return ""
            if args == ["branch", "--no-color"]:
                return "* main\n  gone-branch"
            if args == ["worktree", "list", "--porcelain"]:
                return "worktree /tmp/old-worktree\nHEAD abc123\nbranch refs/heads/gone-branch\n"
            return ""

        gh_merged = subprocess.CompletedProcess([], 0, stdout='[{"number":1}]')
        with (
            patch.object(workspace_mod, "_workspace_dir", return_value=Path("/tmp/ws")),
            patch.object(provision_mod, "_workspace_dir", return_value=Path("/tmp/ws")),
            patch.object(ws_cleanup_mod, "worktree_map", return_value=wt_map),
            patch.object(ws_cleanup_mod, "worktree_branches", return_value={"gone-branch"}),
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "unsynced_commits", return_value=[]),
            patch.object(git_mod, "worktree_remove", return_value=True) as mock_wt_rm,
            patch.object(git_mod, "branch_delete", return_value=True) as mock_br_del,
            patch("teatree.utils.run.subprocess.run", return_value=gh_merged),
        ):
            cleaned = ws_cleanup_mod.prune_branches("/repo")

        assert any("squash-merged" in c for c in cleaned)
        mock_wt_rm.assert_called_once()
        mock_br_del.assert_called_once()


class TestPruneBranchesPassOneAndTwo(TestCase):
    """Cover Pass 1 (gone branches) and Pass 2 (merged branches) in _prune_branches."""

    def test_pass1_deletes_gone_branch_not_in_worktree(self) -> None:
        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return "  active abc123 some work\n  stale-feature def456 [gone] old work"
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return ""
            if args == ["branch", "--no-color"]:
                return "* main"
            return ""

        with (
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "branch_delete") as mock_del,
            patch.object(ws_cleanup_mod, "worktree_branches", return_value=set()),
            patch.object(ws_cleanup_mod, "worktree_map", return_value={}),
        ):
            cleaned = ws_cleanup_mod.prune_branches("/repo")

        mock_del.assert_called_once_with("/repo", "stale-feature")
        assert any("gone" in c and "stale-feature" in c for c in cleaned)

    def test_pass2_deletes_merged_branch_and_skips_protected(self) -> None:
        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return ""
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return "  main\n  merged-feature"
            if args == ["branch", "--no-color"]:
                return "* main"
            return ""

        with (
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "branch_delete") as mock_del,
            patch.object(ws_cleanup_mod, "worktree_branches", return_value=set()),
            patch.object(ws_cleanup_mod, "worktree_map", return_value={}),
        ):
            cleaned = ws_cleanup_mod.prune_branches("/repo")

        mock_del.assert_called_once_with("/repo", "merged-feature")
        assert any("merged" in c and "merged-feature" in c for c in cleaned)

    def test_pass3_warns_non_squash_merged_branch(self) -> None:
        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return ""
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return ""
            if args == ["branch", "--no-color"]:
                return "* main\n  unmerged-feature"
            if "diff" in args:
                return " file.py | 1 +"
            if "rev-list" in args:
                return "5"
            return ""

        gh_no_pr = subprocess.CompletedProcess([], 0, stdout="[]")
        with (
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "branch_delete") as mock_del,
            patch.object(ws_cleanup_mod, "worktree_branches", return_value=set()),
            patch.object(ws_cleanup_mod, "worktree_map", return_value={}),
            patch("teatree.utils.run.subprocess.run", return_value=gh_no_pr),
        ):
            cleaned = ws_cleanup_mod.prune_branches("/repo")

        mock_del.assert_not_called()
        assert any("WARNING" in c and "unmerged-feature" in c for c in cleaned)

    def test_pass1_skips_protected_and_worktree_branches(self) -> None:
        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return "  main abc123 [gone]\n  wt-branch def456 [gone]"
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return ""
            if args == ["branch", "--no-color"]:
                return "* main"
            return ""

        with (
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "branch_delete") as mock_del,
            patch.object(ws_cleanup_mod, "worktree_branches", return_value={"wt-branch"}),
            patch.object(ws_cleanup_mod, "worktree_map", return_value={}),
        ):
            ws_cleanup_mod.prune_branches("/repo")

        mock_del.assert_not_called()


class TestDropOrphanedStashes(TestCase):
    def test_drops_stash_for_deleted_branch(self) -> None:
        stash_output = "stash@{0}: WIP on deleted-branch: abc123 some work"
        branches_output = "* main\n  other-branch"
        calls: list[list[str]] = []

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            calls.append(args)
            if args == ["stash", "list"]:
                return stash_output
            if args == ["branch", "--no-color"]:
                return branches_output
            return ""

        with patch.object(git_mod, "run", side_effect=fake_run):
            result = ws_cleanup_mod.drop_orphaned_stashes("/repo")

        assert len(result) == 1
        assert "deleted-branch" in result[0]
        assert ["stash", "drop", "stash@{0}"] in calls

    def test_keeps_stash_for_existing_branch(self) -> None:
        stash_output = "stash@{0}: WIP on main: abc123 some work"
        branches_output = "* main"

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["stash", "list"]:
                return stash_output
            if args == ["branch", "--no-color"]:
                return branches_output
            return ""

        with patch.object(git_mod, "run", side_effect=fake_run):
            result = ws_cleanup_mod.drop_orphaned_stashes("/repo")

        assert result == []

    def test_returns_empty_when_no_stashes(self) -> None:
        with patch.object(git_mod, "run", return_value=""):
            result = ws_cleanup_mod.drop_orphaned_stashes("/repo")
        assert result == []

    def test_skips_stash_without_on_keyword(self) -> None:
        stash_output = "stash@{0}: Some unusual format"
        branches_output = "* main"

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["stash", "list"]:
                return stash_output
            if args == ["branch", "--no-color"]:
                return branches_output
            return ""

        with patch.object(git_mod, "run", side_effect=fake_run):
            result = ws_cleanup_mod.drop_orphaned_stashes("/repo")

        assert result == []


class TestDropOrphanDatabasesFailure(TestCase):
    def test_returns_empty_when_psql_fails(self) -> None:
        with (
            patch.object(utils_run_mod, "subprocess") as mock_sp,
            patch.object(db_mod, "pg_env", return_value={}),
            patch.object(db_mod, "pg_host", return_value="localhost"),
            patch.object(db_mod, "pg_user", return_value="postgres"),
        ):
            mock_sp.run.return_value = MagicMock(returncode=1)
            result = ws_cleanup_mod.drop_orphan_databases()

        assert result == []


class TestWorktreeBranches(TestCase):
    def test_returns_branch_names_from_worktree_map(self) -> None:
        porcelain = (
            "worktree /home/user/main\n"
            "HEAD abc123\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree /home/user/wt-feature\n"
            "HEAD def456\n"
            "branch refs/heads/feature-branch\n"
        )
        with patch.object(git_mod, "run", return_value=porcelain):
            result = ws_cleanup_mod.worktree_branches("/repo")
        assert result == {"main", "feature-branch"}


class TestWorkspaceFinalize(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_squashes_and_rebases_worktrees(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/90")
        Worktree.objects.create(overlay="test", ticket=ticket, repo_path="/tmp/backend", branch="feature-90")
        Worktree.objects.create(overlay="test", ticket=ticket, repo_path="/tmp/frontend", branch="feature-90")

        with (
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "status_porcelain", return_value=""),
            patch.object(git_mod, "fetch"),
            patch.object(git_mod, "merge_base", return_value="abc123"),
            patch.object(git_mod, "rev_count", return_value=3),
            patch.object(git_mod, "log_oneline", return_value="abc fix: first change\ndef feat: second"),
            patch.object(git_mod, "soft_reset"),
            patch.object(git_mod, "commit"),
            patch.object(git_mod, "rebase"),
        ):
            result = cast("str", call_command("workspace", "finalize", str(ticket.pk)))

        assert "squashed 3 commits" in result
        assert "rebased on main" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_handles_rebase_failure(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/91")
        Worktree.objects.create(overlay="test", ticket=ticket, repo_path="/tmp/backend", branch="feature-91")

        with (
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "status_porcelain", return_value=""),
            patch.object(git_mod, "fetch"),
            patch.object(git_mod, "merge_base", return_value="abc123"),
            patch.object(git_mod, "rev_count", return_value=1),
            patch.object(git_mod, "log_oneline", return_value=""),
            patch.object(
                git_mod,
                "rebase",
                side_effect=utils_run_mod.CommandFailedError(["git", "rebase"], 1, "", "conflict"),
            ),
        ):
            result = cast("str", call_command("workspace", "finalize", str(ticket.pk)))

        assert "rebase failed" in result.lower()
        assert "rebase --abort" in result
        assert "rebase --continue" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_worktree_with_uncommitted_changes(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/95")
        Worktree.objects.create(overlay="test", ticket=ticket, repo_path="/tmp/dirty", branch="feature-95")

        with (
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "status_porcelain", return_value=" M src/file.py"),
        ):
            result = cast("str", call_command("workspace", "finalize", str(ticket.pk)))

        assert "SKIPPED" in result
        assert "uncommitted changes" in result


class TestDropOrphanDatabases(TestCase):
    @override_settings(**SETTINGS)
    def test_drops_orphan_wt_databases(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/64")
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="/tmp/repo",
            branch="feature",
            db_name="wt_known",
        )

        psql_output = "wt_known|postgres|UTF8\nwt_orphan|postgres|UTF8\nother_db|postgres|UTF8\n"

        commands_run: list[list[str]] = []

        def _capture(*args: object, **kwargs: object) -> MagicMock:
            cmd = list(args[0]) if args else []
            commands_run.append(cmd)
            if "psql" in cmd:
                return MagicMock(returncode=0, stdout=psql_output)
            return MagicMock(returncode=0)

        with (
            patch.object(utils_run_mod, "subprocess") as mock_sp,
            patch.object(db_mod, "pg_env", return_value={}),
            patch.object(db_mod, "pg_host", return_value="localhost"),
            patch.object(db_mod, "pg_user", return_value="postgres"),
        ):
            mock_sp.run.side_effect = _capture
            result = ws_cleanup_mod.drop_orphan_databases()

        assert len(result) == 1
        assert "wt_orphan" in result[0]
        dropdb_cmds = [c for c in commands_run if "dropdb" in c]
        assert len(dropdb_cmds) == 1
        assert "wt_orphan" in dropdb_cmds[0]
        # wt_known should NOT be dropped (it's tracked)
        assert not any("wt_known" in c for c in dropdb_cmds)
        # other_db should NOT be dropped (no wt_ prefix)
        assert not any("other_db" in " ".join(c) for c in commands_run if "dropdb" in c)
