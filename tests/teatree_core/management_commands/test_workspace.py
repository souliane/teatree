"""Tests for the workspace and worktree management commands."""

import json
import os
import re
import subprocess
import tempfile
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.module_loading import import_string

import teatree.core.branch_classification as bc_mod
import teatree.core.clean_ignore as clean_ignore_mod
import teatree.core.cleanup as cleanup_mod
import teatree.core.management.commands._workspace_clean_all as ws_clean_all_mod
import teatree.core.management.commands._workspace_cleanup as ws_cleanup_mod
import teatree.core.management.commands._workspace_docker as ws_docker_mod
import teatree.core.management.commands._workspace_salvage as ws_salvage_mod
import teatree.core.management.commands._workspace_stash as ws_stash_mod
import teatree.core.management.commands.workspace as workspace_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.runners.provision as provision_mod
import teatree.core.worktree_done as worktree_done_mod
import teatree.utils.db as db_mod
import teatree.utils.git as git_mod
import teatree.utils.git_commit as git_commit_mod
import teatree.utils.run as utils_run_mod
from teatree.config import load_config
from teatree.core.cleanup_liveness import LivenessVerdict
from teatree.core.management.commands._workspace_ticket_intake import build_branch_name
from teatree.core.management.commands.workspace import _branch_prefix, _workspace_dir
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.overlay import OverlayBase, ProvisionStep
from teatree.core.runners import RunnerResult
from teatree.core.worktree_done import reap_done_worktrees
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


class TestBuildBranchName(TestCase):
    """#1323: branch names follow the flat ``<number>-<description>`` shape.

    No initials prefix (``ac-``/``a-``), no repo segment — those pollute origin
    with orphan refs and force agents into manual cross-branch pushes when the
    local branch disagrees with the MR's source_branch.
    """

    def test_does_not_start_with_initials_or_repo(self) -> None:
        branch = build_branch_name(
            repo_names=["backend", "frontend"],
            ticket_number="1323",
            description="Fix workspace branch prefix",
        )
        assert not branch.startswith("a-")
        assert not branch.startswith("ac-")
        assert not branch.startswith("a/")
        assert not branch.startswith("ac/")
        assert not branch.startswith("backend-")
        assert not branch.startswith("backend/")

    def test_starts_with_ticket_number(self) -> None:
        branch = build_branch_name(
            repo_names=["backend"],
            ticket_number="7485",
            description="bot finding fix",
        )
        assert branch.startswith("7485-")

    def test_no_repo_segment_anywhere(self) -> None:
        branch = build_branch_name(
            repo_names=["api-service", "web-client"],
            ticket_number="8521",
            description="add purpose types",
        )
        # The repo name must not appear as a segment in the branch.
        segments = branch.split("-")
        assert "api" not in segments
        assert "service" not in segments
        assert "web" not in segments
        assert "client" not in segments

    def test_only_lowercase_digits_and_dashes(self) -> None:
        branch = build_branch_name(
            repo_names=["backend"],
            ticket_number="1234",
            description="Add Login Page! With UPPERCASE & symbols",
        )
        assert re.fullmatch(r"[a-z0-9-]+", branch), f"branch {branch!r} contains illegal characters"

    def test_unaffected_by_branch_prefix_env(self) -> None:
        """T3_BRANCH_PREFIX must NOT bleed into the generated branch name (#1323)."""
        with patch.dict("os.environ", {"T3_BRANCH_PREFIX": "ac"}):
            branch = build_branch_name(
                repo_names=["backend"],
                ticket_number="1323",
                description="fix prefix",
            )
        assert not branch.startswith("ac-")
        assert not branch.startswith("ac/")
        assert branch.startswith("1323-")

    def test_description_becomes_slug_after_ticket_number(self) -> None:
        branch = build_branch_name(
            repo_names=["backend"],
            ticket_number="1322",
            description="worktree db link",
        )
        assert branch == "1322-worktree-db-link"

    def test_falls_back_when_description_empty(self) -> None:
        branch = build_branch_name(
            repo_names=["backend"],
            ticket_number="1322",
            description="",
        )
        # Still starts with the ticket number and remains slug-shaped.
        assert branch.startswith("1322-")
        assert re.fullmatch(r"[a-z0-9-]+", branch)


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
    def test_refuses_when_foreign_issue_worktree_dir_exists(self) -> None:
        # #2217 filesystem-evidence guard: a `42-*` dir at a foreign path means
        # someone may already be on the issue; refuse without provisioning.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "42-someone-else-already-here").mkdir()
            with patch.object(workspace_mod, "_workspace_dir", return_value=workspace):
                rc = call_command("workspace", "ticket", "https://example.com/issues/42")
        assert rc == 0
        assert not Ticket.objects.filter(issue_url="https://example.com/issues/42").exists()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_take_over_proceeds_despite_foreign_issue_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "42-someone-else-already-here").mkdir()
            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(workspace_mod, "WorktreeProvisioner") as provisioner,
            ):
                provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="ok")
                rc = call_command("workspace", "ticket", "https://example.com/issues/42", take_over=True)
        assert rc != 0
        assert Ticket.objects.filter(issue_url="https://example.com/issues/42").exists()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_idempotent_reprovision_of_own_dir_is_allowed(self) -> None:
        # Re-provisioning the ticket's OWN existing worktree dir (same path the
        # branch would use) is allowed — the guard only refuses FOREIGN dirs.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(workspace_mod, "WorktreeProvisioner") as provisioner,
            ):
                provisioner.return_value.run.return_value = RunnerResult(ok=True, detail="ok")
                first = call_command("workspace", "ticket", "https://example.com/issues/42")
                # Materialise the ticket's OWN worktree dir on disk (the path the
                # branch resolves to); a re-run must not treat it as a collision.
                own_branch = Ticket.objects.get(pk=first).extra["branch"]
                (workspace / own_branch).mkdir(exist_ok=True)
                second = call_command("workspace", "ticket", "https://example.com/issues/42")
        assert first == second
        assert second != 0

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(
        **SETTINGS,
        TASKS={"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}},
    )
    def test_external_delivery_skips_auto_planner(self) -> None:
        # #2104 acceptance: ``workspace ticket`` is the hand-dispatched
        # external-delivery entry. It stamps the delivery lease and, even when
        # the provision worker runs (immediate backend), the auto-planner is
        # skipped — a directly-implementing delivery agent never claims it.
        from teatree.core.models.external_delivery import under_external_delivery  # noqa: PLC0415

        ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/2104"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert under_external_delivery(ticket) is True
        assert not ticket.tasks.filter(phase="planning").exists()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_variant(self) -> None:
        ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/43", variant="acme"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.variant == "acme"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_repos_with_per_repo_branch_persists_branch_map(self) -> None:
        # #33: a split-branch ticket carries each repo's branch in --repos as
        # `repo:branch`; the map lands on extra['branches'] (repo -> branch) for
        # the provisioner, the repo NAMES drop the suffix, and extra['branch']
        # stays the shared dir name so the repos provision as siblings.
        ticket_id = cast(
            "int",
            call_command(
                "workspace",
                "ticket",
                "https://example.com/issues/44",
                repos="backend:fix/be-44, frontend:fix/fe-44",
            ),
        )

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.repos == ["backend", "frontend"]
        assert ticket.extra["branches"] == {"backend": "fix/be-44", "frontend": "fix/fe-44"}
        assert ticket.extra["branch"].startswith("44-")
        assert ticket.extra["branch"] not in {"fix/be-44", "fix/fe-44"}

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_repos_mixed_bare_and_branch_tokens(self) -> None:
        # A bare `repo` token (no `:branch`) contributes no override — that repo
        # falls back to the shared ticket branch in the provisioner.
        ticket_id = cast(
            "int",
            call_command(
                "workspace",
                "ticket",
                "https://example.com/issues/47",
                repos="backend:fix/be-47, frontend",
            ),
        )

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.repos == ["backend", "frontend"]
        assert ticket.extra["branches"] == {"backend": "fix/be-47"}

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_repos_branch_map_merges_across_reruns(self) -> None:
        # Re-running `ticket` to add a sibling repo's branch keeps the branches
        # already recorded for the earlier repos (merge, not replace).
        call_command(
            "workspace",
            "ticket",
            "https://example.com/issues/45",
            repos="backend:fix/be-45",
        )
        ticket_id = cast(
            "int",
            call_command(
                "workspace",
                "ticket",
                "https://example.com/issues/45",
                repos="frontend:fix/fe-45",
            ),
        )

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.extra["branches"] == {"backend": "fix/be-45", "frontend": "fix/fe-45"}

    def test_parse_repo_branch_map_unit(self) -> None:
        from teatree.core.dev_repo import parse_repo_branch_map  # noqa: PLC0415

        assert parse_repo_branch_map("") == {}
        assert parse_repo_branch_map("backend") == {}  # bare repo, no override
        assert parse_repo_branch_map("a:b") == {"a": "b"}
        # a branch may carry '/'; bare tokens alongside branched ones are skipped.
        assert parse_repo_branch_map("a:fix/b, c, d:main") == {"a": "fix/b", "d": "main"}
        # only the FIRST ':' splits, so a branch may itself contain a ':'.
        assert parse_repo_branch_map("repo:rel:branch") == {"repo": "rel:branch"}

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_rejects_variant_mismatch_on_existing_ticket(self) -> None:
        """Re-issuing `workspace ticket <url> --variant <v>` with a different variant must error (#1306).

        Pre-fix the second invocation silently kept the existing ticket's
        variant and rebound the call to the inferred branch from the URL
        — downstream operations then targeted the wrong code. The fix
        rejects the variant mismatch loudly so the operator knows they
        need to either switch to the existing variant or pick a new
        ticket scope.
        """
        from django.core.management import CommandError  # noqa: PLC0415

        call_command("workspace", "ticket", "https://example.com/issues/1306", variant="client-a")

        with pytest.raises((SystemExit, CommandError)) as exc_info:
            call_command("workspace", "ticket", "https://example.com/issues/1306", variant="client-b")

        if isinstance(exc_info.value, SystemExit):
            assert exc_info.value.code != 0

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

        from teatree.core.gates.orphan_guard import BranchReport, BranchStatus  # noqa: PLC0415

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

            # Pre-create the ticket_dir/backend to simulate existing worktree.
            # #1323: branches follow the flat ``<number>-<description>`` shape.
            branch = "82-ticket"
            ticket_dir = workspace / branch
            ticket_dir.mkdir(parents=True)
            (ticket_dir / "backend").mkdir()

            mock_result = MagicMock()
            mock_result.returncode = 0

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
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
        """Repos in nested subdirectories (e.g. org/backend) are found; worktree branch follows #1323 convention."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            workspace = tmp_path / "workspace"
            (workspace / "org" / "backend").mkdir(parents=True)
            (workspace / "org" / "backend" / ".git").mkdir()
            (workspace / "org" / "frontend").mkdir(parents=True)
            (workspace / "org" / "frontend" / ".git").mkdir()

            def fake_run(cmd: list[str], *_a: object, **_kw: object) -> MagicMock:
                # ``remote get-url origin`` resolves to the slug matching the
                # ``-C <repo>`` clone so the #2276 wrong-repo guard passes.
                stdout = ""
                if "get-url" in cmd and "-C" in cmd:
                    slug = Path(cmd[cmd.index("-C") + 1]).relative_to(workspace)
                    stdout = f"git@github.com:{slug}.git\n"
                return MagicMock(returncode=0, stdout=stdout, stderr="")

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", side_effect=fake_run),
            ):
                ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/90"))

            ticket = Ticket.objects.get(pk=ticket_id)
            assert ticket.worktrees.count() == 2

            repo_paths = sorted(ticket.worktrees.values_list("repo_path", flat=True))
            assert repo_paths == ["org/backend", "org/frontend"]

            # #1323: branch is ``<number>-<slug>``; the repo name is NOT embedded.
            branch = ticket.worktrees.first().branch
            assert "/" not in branch
            assert branch.startswith("90-")
            assert "backend" not in branch.split("-")
            assert "frontend" not in branch.split("-")

    @_patch_overlays(NESTED_OVERLAY)
    @override_settings(**SETTINGS)
    def test_config_workspace_repos_overrides_get_repos(self) -> None:
        """get_workspace_repos() returns config.workspace_repos when set."""
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        overlay = get_overlay()
        assert overlay.get_workspace_repos() == ["org/backend", "org/frontend"]
        assert overlay.get_repos() == ["backend", "frontend"]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-core-issue")
    def test_core_issue_provisions_only_teatree_core_repo(self) -> None:
        """#727: a teatree-core issue URL provisions the core repo, not product repos."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            core = workspace / "souliane" / "teatree"
            (core / ".git").mkdir(parents=True)

            mock_result = MagicMock(returncode=0, stdout="", stderr="")
            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", return_value=mock_result),
                patch("teatree.core.dev_repo.find_project_root", return_value=core),
                patch("teatree.core.dev_repo.discover_active_overlay", return_value=None),
                patch.object(git_mod, "remote_slug", return_value="souliane/teatree"),
            ):
                ticket_id = cast(
                    "int",
                    call_command("workspace", "ticket", "https://github.com/souliane/teatree/issues/727"),
                )

            ticket = Ticket.objects.get(pk=ticket_id)
            assert ticket.repos == ["souliane/teatree"]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS, T3_WORKSPACE_DIR="/tmp/ws-prod-issue")
    def test_product_issue_still_provisions_product_repos(self) -> None:
        """#727 regression guard: a product-repo issue keeps the product repo set."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            core = workspace / "souliane" / "teatree"
            (core / ".git").mkdir(parents=True)
            for repo in ("backend", "frontend"):
                (workspace / repo / ".git").mkdir(parents=True)

            mock_result = MagicMock(returncode=0, stdout="", stderr="")
            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
                patch.object(provision_mod, "_workspace_dir", return_value=workspace),
                patch.object(utils_run_mod.subprocess, "run", return_value=mock_result),
                patch("teatree.core.dev_repo.find_project_root", return_value=core),
                patch("teatree.core.dev_repo.discover_active_overlay", return_value=None),
                patch.object(git_mod, "remote_slug", return_value="souliane/teatree"),
            ):
                ticket_id = cast(
                    "int",
                    call_command("workspace", "ticket", "https://github.com/acme/product-backend/issues/3"),
                )

            ticket = Ticket.objects.get(pk=ticket_id)
            assert ticket.repos == ["backend", "frontend"]


_no_prune = patch.object(ws_clean_all_mod, "prune_branches", new=lambda _repo: [])


_no_stash = patch.object(ws_clean_all_mod, "drop_orphaned_stashes", new=lambda _repo: [])


_no_orphan_dbs = patch.object(ws_clean_all_mod, "drop_orphan_databases", new=list)


_no_orphan_docker = patch.object(ws_clean_all_mod, "reap_orphan_worktree_docker", new=list)


_no_orphan_isolated_roots = patch.object(ws_clean_all_mod, "reap_orphan_isolated_worktree_roots", new=list)


_no_orphan_raw = patch.object(ws_clean_all_mod, "reap_orphan_raw_worktrees", new=lambda _ws: [])


_no_dslr_prune = patch("teatree.utils.django_db.prune_dslr_snapshots", new=lambda **kw: [])


# These integration tests model SETTLED worktrees (cleanup's target). The freshly
# created fixture worktrees would trip the liveness "recent commit" gate, which is
# tested directly in test_cleanup_liveness.py — neutralise it here.
_no_liveness = patch.object(worktree_done_mod, "worktree_liveness", new=lambda *_a, **_k: LivenessVerdict(active=False))


class TestWorkspaceProvisionPositionalId(TestCase):
    """#941: ``workspace provision`` accepts an optional positional ticket id.

    Agents repeatedly typed ``t3 teatree workspace provision <id>`` (habit
    from other overlays) and typer rejected the extra arg with rc=1.
    The command now treats a positional id as a no-op alias for PWD
    auto-detect, exiting 0 on a clean code-only worktree.
    """

    def _make_worktree(self, tmp: str) -> tuple[Ticket, Worktree, Path]:
        wt_dir = Path(tmp) / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/941")
        wt = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_dir)},
            state=Worktree.State.PROVISIONED,
        )
        return ticket, wt, wt_dir

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_positional_ticket_id_accepted(self) -> None:
        """A positional ticket id resolves the ticket directly — no rc=1."""
        with tempfile.TemporaryDirectory() as tmp:
            ticket, _, _wt_dir = self._make_worktree(tmp)
            ok = MagicMock()
            ok.run.return_value = RunnerResult(ok=True, detail="2 step(s) ok")
            with patch.object(workspace_mod, "WorktreeProvisionRunner", return_value=ok):
                # Call WITHOUT path — relies solely on the positional id.
                # Pre-#941 this would raise SystemExit via typer "unexpected argument".
                call_command("workspace", "provision", str(ticket.pk))

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_bogus_id_falls_back_to_path_resolution(self) -> None:
        """A ticket id that doesn't match falls back to PWD/--path resolution."""
        with tempfile.TemporaryDirectory() as tmp:
            _, _, wt_dir = self._make_worktree(tmp)
            ok = MagicMock()
            ok.run.return_value = RunnerResult(ok=True, detail="ok")
            with patch.object(workspace_mod, "WorktreeProvisionRunner", return_value=ok):
                call_command("workspace", "provision", "999999", path=str(wt_dir))

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_id_still_works(self) -> None:
        """Calling without any positional arg still auto-detects (legacy path)."""
        with tempfile.TemporaryDirectory() as tmp:
            _, _, wt_dir = self._make_worktree(tmp)
            ok = MagicMock()
            ok.run.return_value = RunnerResult(ok=True, detail="ok")
            with patch.object(workspace_mod, "WorktreeProvisionRunner", return_value=ok):
                call_command("workspace", "provision", path=str(wt_dir))


class TestWorkspaceStartTeardownExitCodes(TestCase):
    """#932: start/teardown must raise SystemExit(1) on real failures."""

    def _make_worktree(self, tmp: str) -> tuple[Ticket, Worktree, Path]:
        wt_dir = Path(tmp) / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/932")
        wt = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_dir)},
            state=Worktree.State.PROVISIONED,
        )
        return ticket, wt, wt_dir

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_start_failure_raises_system_exit_1(self) -> None:
        """A worktree whose start runner fails must exit 1, not return "error".

        Regression for #932: `return "error"` exited 0, so the lifecycle
        advanced as if every service was up.
        """
        with tempfile.TemporaryDirectory() as tmp:
            _, _, wt_dir = self._make_worktree(tmp)
            failing = MagicMock()
            failing.run.return_value = RunnerResult(ok=False, detail="docker compose up failed")
            with (
                patch.object(workspace_mod, "WorktreeStartRunner", return_value=failing),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("workspace", "start", path=str(wt_dir))
            assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_teardown_with_failures_raises_system_exit_1(self) -> None:
        """Teardown that has per-worktree failures must exit 1.

        Regression for #932: `return f"completed with N failure(s)"` exited 0
        and the wording falsely said "completed".
        """
        with tempfile.TemporaryDirectory() as tmp:
            _, _, wt_dir = self._make_worktree(tmp)
            failing = MagicMock()
            failing.run.return_value = RunnerResult(ok=False, detail="git worktree remove failed")
            with (
                patch.object(workspace_mod, "WorktreeTeardownRunner", return_value=failing),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("workspace", "teardown", path=str(wt_dir))
            assert exc_info.value.code == 1


class TestWorkspaceStartMixedState(TestCase):
    """``workspace start`` tolerates a mixed-state worktree set.

    The command iterates every worktree in the ticket and fires
    ``Worktree.start_services()``. That transition only accepts the
    ``[PROVISIONED, SERVICES_UP, READY]`` source states; a worktree still
    in ``CREATED`` (e.g. a sibling repo whose provision failed/has not run)
    is not a valid source. Pre-fix the unconditional transition raised
    ``django_fsm.TransitionNotAllowed`` on the first CREATED worktree and
    crashed the whole command, leaving the already-startable worktrees in
    whatever partial state the loop had reached. The fix skips worktrees
    that are not in a valid source state and starts the rest.
    """

    def _ticket_with_mixed_worktrees(self, tmp: str) -> tuple[Ticket, Worktree, Worktree, Path]:
        ticket_dir = Path(tmp) / "1234-feature"
        ticket_dir.mkdir()
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1234")

        be_dir = ticket_dir / "backend"
        be_dir.mkdir()
        provisioned = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch="1234-feature",
            extra={"worktree_path": str(be_dir)},
            state=Worktree.State.PROVISIONED,
        )

        fe_dir = ticket_dir / "frontend"
        fe_dir.mkdir()
        created = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="frontend",
            branch="1234-feature",
            extra={"worktree_path": str(fe_dir)},
            state=Worktree.State.CREATED,
        )
        return ticket, provisioned, created, be_dir

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_start_skips_created_worktree_and_starts_the_rest(self) -> None:
        """A CREATED sibling must not crash start; the PROVISIONED one still starts."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket, provisioned, created, be_dir = self._ticket_with_mixed_worktrees(tmp)
            ok = MagicMock()
            ok.run.return_value = RunnerResult(ok=True, detail="ok")
            with patch.object(workspace_mod, "WorktreeStartRunner", return_value=ok):
                # Pre-fix: raises TransitionNotAllowed on the CREATED worktree.
                call_command("workspace", "start", path=str(be_dir))

            provisioned.refresh_from_db()
            created.refresh_from_db()
            # The valid-source worktree DID transition.
            assert provisioned.state == Worktree.State.SERVICES_UP
            # The CREATED worktree was skipped, not transitioned or crashed.
            assert created.state == Worktree.State.CREATED

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_start_does_not_run_start_runner_for_skipped_worktree(self) -> None:
        """The skipped CREATED worktree must not be handed to the start runner."""
        with tempfile.TemporaryDirectory() as tmp:
            _ticket, _provisioned, _created, be_dir = self._ticket_with_mixed_worktrees(tmp)
            started_repos: list[str] = []

            def _runner_factory(worktree: Worktree, **_kwargs: object) -> MagicMock:
                started_repos.append(worktree.repo_path)
                instance = MagicMock()
                instance.run.return_value = RunnerResult(ok=True, detail="ok")
                return instance

            with patch.object(workspace_mod, "WorktreeStartRunner", side_effect=_runner_factory):
                call_command("workspace", "start", path=str(be_dir))

            assert started_repos == ["backend"]


class TestWorkspaceMultiOverlayResolution(TestCase):
    """#1310: workspace subcommands disambiguate overlays from the ticket row.

    When two overlays are installed and ``T3_OVERLAY_NAME`` is NOT set in
    the subprocess env (a real path that happens when the env var is lost,
    or when a future call site bypasses the CLI bridge), the workspace
    subcommands ``provision``/``start``/``ready``/``teardown`` used to die
    with ``ImproperlyConfigured: Multiple overlays found``. The ticket
    itself stores the overlay name in ``Ticket.overlay`` — passing that
    through to ``get_overlay(name)`` is the unambiguous resolution and
    removes the env-var dependence.
    """

    def _make_worktree(self, tmp: str, overlay_name: str = "alpha") -> tuple[Ticket, Worktree, Path]:
        wt_dir = Path(tmp) / "backend"
        wt_dir.mkdir()
        ticket = Ticket.objects.create(
            overlay=overlay_name,
            issue_url="https://example.com/issues/1310",
        )
        wt = Worktree.objects.create(
            overlay=overlay_name,
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_dir)},
            state=Worktree.State.PROVISIONED,
        )
        return ticket, wt, wt_dir

    @staticmethod
    def _patch_two_overlays():
        """Return a patch that exposes ``alpha`` and ``beta`` overlays.

        Mirrors ``_patch_overlays`` but registers two overlays so the
        ambiguous ``get_overlay()`` resolution would fail; ``ticket.overlay``
        is the only signal that breaks the tie.
        """
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        alpha = FullOverlay()
        beta = FullOverlay()
        result: dict[str, OverlayBase] = {"alpha": alpha, "beta": beta}

        def _fake_discover() -> dict[str, OverlayBase]:
            return result

        _fake_discover.cache_clear = lambda: None
        return patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover)

    @override_settings(**SETTINGS)
    def test_provision_resolves_overlay_from_ticket_without_env_var(self) -> None:
        """``workspace provision`` survives a missing ``T3_OVERLAY_NAME`` env."""
        with self._patch_two_overlays(), tempfile.TemporaryDirectory() as tmp:
            ticket, _wt, _wt_dir = self._make_worktree(tmp, overlay_name="alpha")
            ok = MagicMock()
            ok.run.return_value = RunnerResult(ok=True, detail="2 step(s) ok")
            env_without_overlay = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
            with (
                patch.dict(os.environ, env_without_overlay, clear=True),
                patch.object(workspace_mod, "WorktreeProvisionRunner", return_value=ok),
            ):
                # Pre-fix: raises ImproperlyConfigured(Multiple overlays found …).
                # Post-fix: resolves from ``ticket.overlay`` = "alpha".
                call_command("workspace", "provision", str(ticket.pk))

    @override_settings(**SETTINGS)
    def test_start_resolves_overlay_from_ticket_without_env_var(self) -> None:
        """``workspace start`` survives a missing ``T3_OVERLAY_NAME`` env."""
        with self._patch_two_overlays(), tempfile.TemporaryDirectory() as tmp:
            _ticket, _wt, wt_dir = self._make_worktree(tmp, overlay_name="alpha")
            ok = MagicMock()
            ok.run.return_value = RunnerResult(ok=True, detail="ok")
            env_without_overlay = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
            with (
                patch.dict(os.environ, env_without_overlay, clear=True),
                patch.object(workspace_mod, "WorktreeStartRunner", return_value=ok),
            ):
                call_command("workspace", "start", path=str(wt_dir))

    @override_settings(**SETTINGS)
    def test_ready_resolves_overlay_from_ticket_without_env_var(self) -> None:
        """``workspace ready`` survives a missing ``T3_OVERLAY_NAME`` env."""
        with self._patch_two_overlays(), tempfile.TemporaryDirectory() as tmp:
            _ticket, _wt, wt_dir = self._make_worktree(tmp, overlay_name="alpha")
            env_without_overlay = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
            with patch.dict(os.environ, env_without_overlay, clear=True):
                # ``alpha`` returns no readiness probes (FullOverlay default), so
                # this call asserts the resolution path; no SystemExit because
                # no probes ran => total_failures == 0.
                call_command("workspace", "ready", path=str(wt_dir))

    @override_settings(**SETTINGS)
    def test_resolve_overlay_name_for_url_helper_routes_through_inference(self) -> None:
        """``_resolve_overlay_name_for_url`` returns the correct overlay name.

        Unit-level: validates the resolution helper that the ``ticket``
        command leans on when ``T3_OVERLAY_NAME`` is missing. ``alpha``
        claims any URL containing ``alpha-corp/<repo>``, ``beta`` claims
        ``beta-corp/<repo>``. Mirrors what ``get_overlay(name)`` would
        receive on the production code path.
        """
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        class AlphaOverlay(FullOverlay):
            def get_workspace_repos(self) -> list[str]:
                return ["alpha-corp/backend", "alpha-corp/frontend"]

        class BetaOverlay(FullOverlay):
            def get_workspace_repos(self) -> list[str]:
                return ["beta-corp/api", "beta-corp/web"]

        result: dict[str, OverlayBase] = {"alpha": AlphaOverlay(), "beta": BetaOverlay()}

        def _fake_discover() -> dict[str, OverlayBase]:
            return result

        _fake_discover.cache_clear = lambda: None

        env_without_overlay = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
        with (
            patch.dict(os.environ, env_without_overlay, clear=True),
            patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover),
        ):
            from teatree.core.management.commands._workspace_helpers import (  # noqa: PLC0415
                resolve_overlay_name_for_url,
            )

            assert resolve_overlay_name_for_url("https://example.com/alpha-corp/backend/issues/77") == "alpha"
            assert resolve_overlay_name_for_url("https://example.com/beta-corp/api/issues/88") == "beta"
            # No overlay claims the URL → ``None`` (caller surfaces the
            # ambiguity error from ``get_overlay`` with the actual list).
            assert resolve_overlay_name_for_url("https://example.com/unknown-corp/repo/issues/99") is None

        # When ``T3_OVERLAY_NAME`` is set, the helper defers to ``get_overlay``
        # (returns ``None`` so ``get_overlay(None)`` reads the env var itself).
        with (
            patch.dict(os.environ, {**env_without_overlay, "T3_OVERLAY_NAME": "beta"}, clear=True),
            patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover),
        ):
            from teatree.core.management.commands._workspace_helpers import (  # noqa: PLC0415
                resolve_overlay_name_for_url,
            )

            assert resolve_overlay_name_for_url("https://example.com/alpha-corp/backend/issues/77") is None

    @override_settings(**SETTINGS)
    def test_ticket_stamps_true_owner_overlay_not_bare_path_sibling(self) -> None:
        """``workspace ticket`` stamps the slug-owning overlay, not a bare-path sibling (#1120).

        End-to-end at the command seam: with both ``t3-teatree`` (whose
        ``get_workspace_repos()`` carries the bare relative path
        ``t3-company`` exactly as ``_discover_workspace_repos()`` emits it)
        and ``t3-company`` (whose list carries the proper ``owner/name``
        slug) registered and ``T3_OVERLAY_NAME`` unset, a ticket for the
        reporter's ``company-fork-org/t3-company`` URL must be attributed to
        ``t3-company``. Pre-fix the raw-substring match made the first
        dict hit (``t3-teatree``) win, poisoning every later step.
        """
        from teatree.core.overlay import OverlayBase  # noqa: PLC0415

        class TeatreeSibling(FullOverlay):
            def get_workspace_repos(self) -> list[str]:
                return ["teatree", "t3-company"]

        class CompanyOverlay(FullOverlay):
            def get_workspace_repos(self) -> list[str]:
                return ["company-fork-org/t3-company"]

        result: dict[str, OverlayBase] = {
            "t3-teatree": TeatreeSibling(),
            "t3-company": CompanyOverlay(),
        }

        def _fake_discover() -> dict[str, OverlayBase]:
            return result

        _fake_discover.cache_clear = lambda: None

        url = "https://github.com/company-fork-org/t3-company/issues/147"
        env_without_overlay = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
        provisioner = MagicMock()
        provisioner.run.return_value = RunnerResult(ok=True, detail="ok")
        with (
            patch.dict(os.environ, env_without_overlay, clear=True),
            patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover),
            patch.object(workspace_mod, "WorktreeProvisioner", return_value=provisioner),
        ):
            ticket_id = cast("int", call_command("workspace", "ticket", url, repos="backend"))

        ticket = Ticket.objects.get(pk=ticket_id)
        assert ticket.overlay == "t3-company"

    @override_settings(**SETTINGS)
    def test_teardown_does_not_need_overlay_resolution(self) -> None:
        """``workspace teardown`` does not call ``get_overlay()`` on the hot path.

        The teardown runner only consults the worktree row (db_name, extra
        snapshot, force flag) — no overlay hooks. The bare ``get_overlay()``
        call at line 367 was on the (now-removed) `ready`-style header; once
        the multi-overlay fix re-routes resolution through ``ticket.overlay``,
        teardown stays unaffected and survives a missing env var trivially.
        """
        with self._patch_two_overlays(), tempfile.TemporaryDirectory() as tmp:
            _ticket, _wt, wt_dir = self._make_worktree(tmp, overlay_name="alpha")
            ok = MagicMock()
            ok.run.return_value = RunnerResult(ok=True, detail="torn down")
            env_without_overlay = {k: v for k, v in os.environ.items() if k != "T3_OVERLAY_NAME"}
            with (
                patch.dict(os.environ, env_without_overlay, clear=True),
                patch.object(workspace_mod, "WorktreeTeardownRunner", return_value=ok),
            ):
                call_command("workspace", "teardown", path=str(wt_dir))


class TestWorkspaceEmitAndSalvage(TestCase):
    """The ``workspace emit`` (structured handoff) and ``workspace salvage`` CLI entries (#2763)."""

    def test_emit_prints_json_array(self) -> None:
        with patch.object(workspace_mod, "_workspace_dir", return_value=Path("/ws")):
            rendered = cast("str", call_command("workspace", "emit"))
        assert json.loads(rendered) == [], "no NOT-auto-deleted items → empty JSON array"

    def test_emit_serialises_collected_records(self) -> None:
        from teatree.core.cleanup_emit import CleanupEmitRecord  # noqa: PLC0415

        record = CleanupEmitRecord(path="/ws/feat", branch="feat", kind="worktree", unique_commit_shas=["abc"])
        with (
            patch.object(workspace_mod, "_workspace_dir", return_value=Path("/ws")),
            patch.object(ws_salvage_mod, "collect_emit_records", return_value=[record]),
        ):
            data = json.loads(cast("str", call_command("workspace", "emit")))
        assert data[0]["branch"] == "feat"
        assert data[0]["unique_commit_shas"] == ["abc"]
        assert data[0]["schema_version"] == 1

    def test_salvage_builds_request_and_reports_outcome(self) -> None:
        from teatree.core.cleanup_salvage import SalvageRequest, SalvageResult  # noqa: PLC0415

        captured: dict[str, object] = {}

        def _fake_salvage(request: SalvageRequest, _hooks: object) -> SalvageResult:
            captured["source_ref"] = request.source_ref
            captured["salvage_branch"] = request.salvage_branch
            return SalvageResult(salvaged=True, deleted=True, pr_url="https://x/pr/9", salvage_branch="salvage/feat")

        with (
            patch.object(ws_salvage_mod.git, "run", return_value="/repo"),
            patch.object(ws_salvage_mod, "salvage_item", side_effect=_fake_salvage),
        ):
            line = cast("str", call_command("workspace", "salvage", "feat"))

        assert captured["source_ref"] == "feat"
        assert captured["salvage_branch"] == "salvage/feat", "defaults to salvage/<source_ref>"
        assert "salvaged=True" in line
        assert "deleted=True" in line


class TestCleanAllDryRun(TestCase):
    """``clean-all --dry-run`` previews the done-reaper and removes nothing."""

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_orphan_isolated_roots
    @_no_orphan_docker
    @_no_dslr_prune
    @_no_orphan_raw
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_dry_run_skips_the_destructive_passes(self) -> None:
        reaper_calls: dict[str, bool] = {}

        def _spy(_ws: Path, *, dry_run: bool) -> list[str]:
            reaper_calls["dry_run"] = dry_run
            return ["WOULD WIPE 'feat': done (ticket-state:merged), all changes proven redundant"]

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(workspace_mod, "_workspace_dir", return_value=Path(tmp)),
            patch.object(ws_clean_all_mod, "reap_done_worktrees", side_effect=_spy),
            patch.object(ws_clean_all_mod, "drop_orphan_databases") as mock_drop,
        ):
            cleaned = cast("list[str]", call_command("workspace", "clean-all", "--dry-run"))

        assert reaper_calls["dry_run"] is True
        assert any("WOULD WIPE" in line for line in cleaned)
        mock_drop.assert_not_called()  # dry-run touches nothing beyond the preview


@_no_orphan_raw
class TestWorkspaceCleanAll(TestCase):
    """End-to-end ``clean-all`` CLI integration over the consolidated done-reaper.

    The per-worktree done-detection + analyze-before-wipe depth lives in
    ``tests/teatree_core/test_worktree_done.py`` (real git, every disposition);
    full-stack reap/keep in :class:`TestCleanAllReapsAndSurvivesForeignOverlay`.
    These pin the remaining CLI-level concerns: overlay cleanup steps fire while
    wiping a done worktree, and the secondary passes (empty-dir prune, DSLR prune,
    in-use-tenant skip, orphan-docker reap) are sequenced and reported.
    """

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_orphan_isolated_roots
    @_no_orphan_docker
    @_no_dslr_prune
    @_no_liveness
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_overlay_cleanup_steps_on_a_reaped_worktree(self) -> None:
        """clean-all invokes overlay.get_cleanup_steps() while wiping a done worktree."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _make_squash_merged_worktree(tmp, ticket_number="86")

            cleanup_called: list[bool] = []

            class CleanupOverlay(FullOverlay):
                def get_cleanup_steps(self, worktree: Worktree) -> list[ProvisionStep]:
                    return [ProvisionStep(name="docker-down", callable=lambda: cleanup_called.append(True))]

            result: dict[str, OverlayBase] = {"test": CleanupOverlay()}

            def _fake_discover() -> dict[str, OverlayBase]:
                return result

            _fake_discover.cache_clear = lambda: None

            with (
                patch.object(workspace_mod, "_workspace_dir", return_value=tmp),
                patch.object(provision_mod, "_workspace_dir", return_value=tmp),
                patch.object(overlay_loader_mod, "_discover_overlays", new=_fake_discover),
                patch.object(cleanup_mod, "drop_db"),
                patch("teatree.core.runners.worktree_start.docker_compose_down"),
            ):
                call_command("workspace", "clean-all")

            assert cleanup_called == [True], "overlay cleanup steps must run while reaping a done worktree"
            assert Worktree.objects.count() == 0, "the squash-merged worktree must be reaped"

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_orphan_isolated_roots
    @_no_orphan_docker
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
    @_no_orphan_isolated_roots
    @_no_orphan_docker
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
    @_no_orphan_isolated_roots
    @_no_orphan_docker
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_passes_in_use_tenants_from_active_worktrees(self) -> None:
        """clean-all collects DSLR tenants from CREATED worktrees and skips them (#1306).

        A worktree in ``CREATED`` state is mid-provision — its DB has not
        yet been imported and it depends on the tenant's DSLR snapshot
        remaining intact. Pre-fix clean-all pruned unconditionally and
        destroyed snapshots that an in-flight worktree was about to
        restore from. The fix collects active variants from CREATED
        worktrees and passes them to ``prune_dslr_snapshots`` via the
        new ``in_use_tenants`` kwarg.
        """
        captured: dict[str, object] = {}

        def fake_prune(**kw: object) -> list[str]:
            captured.update(kw)
            return []

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(workspace_mod, "_workspace_dir", return_value=Path(tmp)),
            patch.object(provision_mod, "_workspace_dir", return_value=Path(tmp)),
            patch("teatree.utils.django_db.prune_dslr_snapshots", side_effect=fake_prune),
        ):
            ticket = Ticket.objects.create(
                overlay="test", issue_url="https://example.com/issues/1306", variant="tenant-a"
            )
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="ac-1306",
                state=Worktree.State.CREATED,
                extra={},
            )
            call_command("workspace", "clean-all")

        # Default overlay returns the variant verbatim; the in-use tenant set
        # carries the active variant string so the pruner skips it.
        assert captured.get("in_use_tenants") == {"tenant-a"}

    @_no_prune
    @_no_stash
    @_no_orphan_dbs
    @_no_orphan_isolated_roots
    @_no_dslr_prune
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_reaps_orphan_worktree_docker(self) -> None:
        """#1523: clean-all reaps docker for compose projects whose worktree dir is gone."""
        with patch.object(ws_clean_all_mod, "reap_orphan_worktree_docker") as mock_reap:
            mock_reap.return_value = ["Reaped docker project teatree-wt99: 1 container(s), 1 image(s)"]
            cleaned = cast("list[str]", call_command("workspace", "clean-all"))

        mock_reap.assert_called_once_with()
        assert any("Reaped docker project teatree-wt99" in c for c in cleaned)


class TestReapOrphanWorktreeDocker(TestCase):
    """#1523 orphan reaper: a worktree whose dir is gone is not live, so its docker is reaped.

    The docker subprocess is mocked at the engine boundary
    (``reap_orphan_compose_projects``); this asserts the live/keep set is
    computed correctly from the rows-on-disk and handed to the engine.
    """

    def _worktree(self, *, repo: str, number: str, wt_path: str | None) -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url=f"https://example.com/issues/{number}")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path=repo,
            branch=f"{number}-x",
            extra={"worktree_path": wt_path} if wt_path else {},
        )

    def test_live_set_excludes_worktrees_whose_dir_is_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp) / "live"
            live_dir.mkdir()
            self._worktree(repo="backend", number="1", wt_path=str(live_dir))
            self._worktree(repo="backend", number="9", wt_path=str(Path(tmp) / "gone"))

            with patch.object(ws_docker_mod, "reap_orphan_compose_projects", return_value=[]) as mock_engine:
                ws_docker_mod.reap_orphan_worktree_docker()

        (live_projects,) = mock_engine.call_args.args
        assert live_projects == {"backend-wt1"}

    def test_renders_engine_results_as_lines(self) -> None:
        from teatree.docker.reap import ReapResult  # noqa: PLC0415

        result = ReapResult(project="backend-wt9", containers_removed=2, images_removed=1)
        with patch.object(ws_docker_mod, "reap_orphan_compose_projects", return_value=[result]):
            lines = ws_docker_mod.reap_orphan_worktree_docker()

        assert lines == [str(result)]
        assert "backend-wt9" in lines[0]


class TestWorkspaceCleanMerged(TestCase):
    """``clean-merged`` delegates to the consolidated done-worktree reaper.

    The deep done-detection + analyze-before-wipe behaviour lives in
    ``tests/teatree_core/test_worktree_done.py``; here we only assert the CLI
    routes to :func:`reap_done_worktrees` (the live, non-dry-run path).
    """

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_worktrees_returns_empty(self) -> None:
        cleaned = cast("list[str]", call_command("workspace", "clean-merged"))
        assert cleaned == []

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_delegates_to_the_done_reaper(self) -> None:
        with patch.object(workspace_mod, "reap_done_worktrees", return_value=["Wiped 'ac-repo-70'"]) as mock_reap:
            cleaned = cast("list[str]", call_command("workspace", "clean-merged"))

        mock_reap.assert_called_once()
        assert mock_reap.call_args.kwargs.get("dry_run") is False
        assert cleaned == ["Wiped 'ac-repo-70'"]


class TestPruneBranches(TestCase):
    # The canonical layered ``is_squash_merged`` / ``branch_redundancy`` detection
    # (cherry-zero / synthetic-squash / ``--merged`` / forge-corroborating-only) is
    # exercised end-to-end against real git in ``tests/teatree_core/cleanup/
    # test_branch_redundancy.py`` and ``TestIsSquashMergedRealGit`` — a mocked
    # ``git.run`` cannot model the multi-command layered detector.

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
    @_no_orphan_isolated_roots
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
            # The layered detection is real-git-tested in test_branch_redundancy.py; here
            # the branch IS classified squash-merged so the Pass-3 reap/block path is exercised.
            patch.object(ws_cleanup_mod, "is_squash_merged", return_value=True),
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_commit_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            # Squash-merged: the content is on the remote, nothing absent (#710).
            patch.object(git_mod, "commits_absent_from_all_remotes", return_value=[]),
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
            # The layered detection is real-git-tested in test_branch_redundancy.py; here
            # the branch IS classified squash-merged so the Pass-3 reap/block path is exercised.
            patch.object(ws_cleanup_mod, "is_squash_merged", return_value=True),
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
            # The layered detection is real-git-tested in test_branch_redundancy.py; here
            # the branch IS classified squash-merged so the Pass-3 reap/block path is exercised.
            patch.object(ws_cleanup_mod, "is_squash_merged", return_value=True),
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "unsynced_commits", return_value=[]),
            # Fully synced: no commits absent from any remote (#706/#710 guard).
            patch.object(git_mod, "commits_absent_from_all_remotes", return_value=[]),
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

    def test_pass1_strips_current_branch_marker_on_gone_branch(self) -> None:
        # `git branch -v` prefixes the checked-out branch with "* ". A gone
        # current branch reads "* feature abc123 [gone] ...". Parsing must
        # recover "feature" (which is protected as the current branch), never
        # the literal "*" — passing "*" to branch_delete is both wrong and
        # dangerous (git interprets it as a refspec glob).
        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return "* feature abc123 [gone] some work"
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return ""
            if args == ["branch", "--no-color"]:
                return "* feature"
            return ""

        with (
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="feature"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "branch_delete") as mock_del,
            patch.object(ws_cleanup_mod, "worktree_branches", return_value=set()),
            patch.object(ws_cleanup_mod, "worktree_map", return_value={}),
        ):
            ws_cleanup_mod.prune_branches("/repo")

        # The marker must resolve to "feature", which is the protected current
        # branch — so nothing is deleted at all. The buggy parser yielded "*",
        # which is unprotected, so it reached branch_delete (and "*" is a
        # dangerous refspec glob). assert_not_called covers both failures.
        mock_del.assert_not_called()


class TestPruneBranchesHonorsCleanIgnore(TestCase):
    """clean_ignore must be honored on the branch-deletion paths, not only the row reaper.

    Pre-fix ``prune_branches`` consulted clean_ignore nowhere, so a
    clean_ignore-matching branch that classified as squash-merged was deleted
    despite being a never-merge dev override. The shared predicate now protects
    it across every deletion pass.
    """

    def _patch_clean_ignore(self, patterns: list[str]) -> AbstractContextManager[object]:
        patched = replace(load_config().user, clean_ignore=patterns)
        return patch.object(clean_ignore_mod, "get_effective_settings", return_value=patched)

    def test_clean_ignore_squash_merged_branch_survives(self) -> None:
        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            if args == ["branch", "-v", "--no-color"]:
                return ""
            if args == ["branch", "--merged", "origin/main", "--no-color"]:
                return ""
            if args == ["branch", "--no-color"]:
                return "* main\n  dev-override"
            if "diff" in args:
                return ""  # empty diff => is_squash_merged True via fallback
            if "rev-list" in args:
                return "3"
            return ""

        with (
            self._patch_clean_ignore(["dev-override"]),
            patch.object(git_mod, "run", side_effect=fake_run),
            patch.object(git_mod, "current_branch", return_value="main"),
            patch.object(git_mod, "default_branch", return_value="main"),
            patch.object(git_mod, "branch_delete") as mock_del,
            patch.object(ws_cleanup_mod, "worktree_branches", return_value=set()),
            patch.object(ws_cleanup_mod, "worktree_map", return_value={}),
        ):
            cleaned = ws_cleanup_mod.prune_branches("/repo")

        mock_del.assert_not_called()
        assert not any("dev-override" in c and "WARNING" in c for c in cleaned)


class TestReapHonorsPerOverlayCleanIgnore(TestCase):
    """The done reaper must resolve clean_ignore per the worktree's own overlay.

    Pre-fix it read the raw global ``load_config().user.clean_ignore``, so a
    pattern set only under ``[overlays.<name>]`` was dead — the per-overlay
    override never reached the keep decision. The single ``is_clean_ignored``
    predicate (in :mod:`teatree.core.clean_ignore`) resolves
    ``get_effective_settings(worktree.overlay).clean_ignore`` per row, and
    ``reap_done_worktree`` checks it FIRST — before the done/analyze gates.
    """

    def _make_row(self, work: Path, wt_dir: Path, *, overlay: str, branch: str) -> Worktree:
        ticket = Ticket.objects.create(overlay=overlay, issue_url="https://example.com/issues/1")
        return Worktree.objects.create(
            overlay=overlay,
            ticket=ticket,
            repo_path="work",
            branch=branch,
            state=Worktree.State.PROVISIONED,
            extra={"worktree_path": str(wt_dir), "clone_path": str(work)},
        )

    def test_per_overlay_pattern_keeps_matching_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            workspace = tmp / "workspace"
            workspace.mkdir()
            work, _tip = _squash_merge_into_main(workspace, subject="feat: shipped (#1)")
            wt_dir = workspace / "spike" / "work"
            _git(work, "branch", "-m", "feature", "spike-x")
            _git(work, "worktree", "add", "-q", str(wt_dir), "spike-x")
            row = self._make_row(work, wt_dir, overlay="heavy", branch="spike-x")

            def fake_effective(name: str | None = None) -> object:
                base = load_config().user
                patterns = ["spike-*"] if name == "heavy" else []
                return replace(base, clean_ignore=patterns)

            with patch.object(clean_ignore_mod, "get_effective_settings", side_effect=fake_effective):
                cleaned = reap_done_worktrees(workspace, dry_run=False)

            assert Worktree.objects.filter(pk=row.pk).exists(), (
                f"per-overlay clean_ignore must keep the row; got: {cleaned!r}"
            )
            assert wt_dir.is_dir()
            assert any("SKIP" in c and "spike-x" in c for c in cleaned)


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
            result = ws_stash_mod.drop_orphaned_stashes("/repo")

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
            result = ws_stash_mod.drop_orphaned_stashes("/repo")

        assert result == []

    def test_returns_empty_when_no_stashes(self) -> None:
        with patch.object(git_mod, "run", return_value=""):
            result = ws_stash_mod.drop_orphaned_stashes("/repo")
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
            result = ws_stash_mod.drop_orphaned_stashes("/repo")

        assert result == []

    def test_drops_explicit_message_stash_for_deleted_branch(self) -> None:
        # `git stash push -m "msg"` produces "On <branch>: <msg>" (capital On,
        # no lowercase " on " token). The branch is deleted, so this is a
        # genuine orphan that must be dropped.
        stash_output = "stash@{0}: On deleted-branch: refactor parser"
        branches_output = "* main"
        calls: list[list[str]] = []

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            calls.append(args)
            if args == ["stash", "list"]:
                return stash_output
            if args == ["branch", "--no-color"]:
                return branches_output
            return ""

        with patch.object(git_mod, "run", side_effect=fake_run):
            result = ws_stash_mod.drop_orphaned_stashes("/repo")

        assert len(result) == 1
        assert "deleted-branch" in result[0]
        assert ["stash", "drop", "stash@{0}"] in calls

    def test_keeps_stash_for_existing_branch_with_on_in_message(self) -> None:
        # The stash message contains the word "on"; a naive split on " on "
        # mis-parses the branch as "the login flow" and would drop a stash that
        # still belongs to the existing branch.
        stash_output = "stash@{0}: On kept-branch: working on the login flow"
        branches_output = "* main\n  kept-branch"
        calls: list[list[str]] = []

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            calls.append(args)
            if args == ["stash", "list"]:
                return stash_output
            if args == ["branch", "--no-color"]:
                return branches_output
            return ""

        with patch.object(git_mod, "run", side_effect=fake_run):
            result = ws_stash_mod.drop_orphaned_stashes("/repo")

        assert result == []
        assert not any(a[:2] == ["stash", "drop"] for a in calls)

    def test_keeps_orphaned_stash_when_changes_not_merged(self) -> None:
        # An orphaned stash whose branch is gone but whose changes are NOT captured
        # upstream must be KEPT — dropping it is silent data loss (the #1913 FSM /
        # dreaming-phase stashes). `git cherry` prints `+` for the unmerged commit.
        stash_output = "stash@{0}: WIP on deleted-branch: abc123 unmerged work"
        branches_output = "* main"
        calls: list[list[str]] = []

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            calls.append(args)
            if args == ["stash", "list"]:
                return stash_output
            if args == ["branch", "--no-color"]:
                return branches_output
            if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
                return "origin/main"
            if args[:1] == ["cherry"]:
                return "+ abc123def unmerged work"
            return ""

        with patch.object(git_mod, "run", side_effect=fake_run):
            result = ws_stash_mod.drop_orphaned_stashes("/repo")

        assert len(result) == 1
        assert "Kept orphaned stash" in result[0]
        assert "deleted-branch" in result[0]
        assert not any(a[:2] == ["stash", "drop"] for a in calls), "must NOT drop an unmerged orphaned stash"

    def test_drops_orphaned_stash_when_changes_already_merged(self) -> None:
        # The symmetric case: an orphaned stash whose changes ARE captured upstream
        # (`git cherry` prints `-`) is safe to drop — nothing is lost.
        stash_output = "stash@{0}: WIP on deleted-branch: abc123 merged work"
        branches_output = "* main"
        calls: list[list[str]] = []

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            calls.append(args)
            if args == ["stash", "list"]:
                return stash_output
            if args == ["branch", "--no-color"]:
                return branches_output
            if args == ["rev-parse", "--abbrev-ref", "origin/HEAD"]:
                return "origin/main"
            if args[:1] == ["cherry"]:
                return "- abc123def merged work"
            return ""

        with patch.object(git_mod, "run", side_effect=fake_run):
            result = ws_stash_mod.drop_orphaned_stashes("/repo")

        assert len(result) == 1
        assert "already merged" in result[0]
        assert ["stash", "drop", "stash@{0}"] in calls

    def test_keeps_detached_head_stash(self) -> None:
        # A stash taken on a detached HEAD reads "On (no branch): ..." — there
        # is no owning branch, so it must be kept rather than reaped.
        stash_output = "stash@{0}: On (no branch): detached work"
        branches_output = "* main"
        calls: list[list[str]] = []

        def fake_run(*, repo: str = ".", args: list[str]) -> str:
            calls.append(args)
            if args == ["stash", "list"]:
                return stash_output
            if args == ["branch", "--no-color"]:
                return branches_output
            return ""

        with patch.object(git_mod, "run", side_effect=fake_run):
            result = ws_stash_mod.drop_orphaned_stashes("/repo")

        assert result == []
        assert not any(a[:2] == ["stash", "drop"] for a in calls)


class TestStashBranch(TestCase):
    def test_parses_wip_auto_stash(self) -> None:
        line = "stash@{0}: WIP on feature-x: abc123 init"
        assert ws_stash_mod._stash_branch(line) == "feature-x"

    def test_parses_explicit_message_stash(self) -> None:
        line = "stash@{2}: On feature-x: working on the parser"
        assert ws_stash_mod._stash_branch(line) == "feature-x"

    def test_unparseable_line_returns_empty(self) -> None:
        assert ws_stash_mod._stash_branch("stash@{0}: Some unusual format") == ""

    def test_detached_head_returns_empty(self) -> None:
        assert ws_stash_mod._stash_branch("stash@{0}: On (no branch): detached work") == ""


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


# A deterministic git identity for tests. The CI image (dev/Dockerfile.test,
# Ubuntu, user ``testuser``) configures no ``user.name``/``user.email`` and,
# unlike a dev box's git, cannot auto-detect one — so any ``git commit`` with no
# identity in the environment aborts with rc=128 "Author identity unknown". A
# test must therefore inject this identity into the environment of *every* commit
# it triggers, including the one ``workspace finalize`` runs as a subprocess.
_GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with a deterministic identity, returning stdout."""
    env = {**os.environ, **_GIT_IDENTITY_ENV}
    out = subprocess.run(
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return out.stdout.strip()


def _init_repo_with_remote(tmp: Path) -> tuple[Path, Path]:
    """Create a work repo with a pushed ``main`` and return ``(remote, work)``."""
    remote = tmp / "remote.git"
    work = tmp / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)  # noqa: S607
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)  # noqa: S607
    _git(work, "commit", "-q", "--allow-empty", "-m", "base")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "fetch", "-q", "origin")
    return remote, work


class TestWorkspaceFinalizeMainCloneGuard(TestCase):
    """Defense-in-depth: finalize must never commit on a managed main clone (#752)."""

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_finalize_refuses_commit_on_main_clone_default_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _remote, clone = _init_repo_with_remote(Path(tmp))
            # Two committed changes on the default branch with a clean tree:
            # absent the guard, finalize would soft-reset + commit (squash) here.
            (clone / "feature.py").write_text("x = 1\n")
            _git(clone, "add", "feature.py")
            _git(clone, "commit", "-q", "-m", "first change")
            (clone / "feature2.py").write_text("y = 2\n")
            _git(clone, "add", "feature2.py")
            _git(clone, "commit", "-q", "-m", "second change")

            head_before = _git(clone, "rev-parse", "HEAD")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/752")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path=str(clone),
                branch="main",
                extra={"worktree_path": str(clone)},
            )

            with pytest.raises(SystemExit) as exc:
                call_command("workspace", "finalize", str(ticket.pk))

            assert "main clone" in str(exc.value).lower()
            assert "worktree" in str(exc.value).lower()
            # No commit was created on the main clone.
            assert _git(clone, "rev-parse", "HEAD") == head_before

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_finalize_commits_in_real_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _remote, clone = _init_repo_with_remote(tmp_path)

            wt = tmp_path / "wt-feature"
            _git(clone, "worktree", "add", "-q", "-b", "feature-x", str(wt))
            # Two commits so finalize squashes them (exercises the commit path).
            (wt / "a.py").write_text("a = 1\n")
            _git(wt, "add", "a.py")
            _git(wt, "commit", "-q", "-m", "first change")
            (wt / "b.py").write_text("b = 2\n")
            _git(wt, "add", "b.py")
            _git(wt, "commit", "-q", "-m", "second change")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/753")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path=str(wt),
                branch="feature-x",
                extra={"worktree_path": str(wt)},
            )

            # finalize squashes via a real ``git commit`` subprocess, which
            # inherits this process's environment — give it the same identity
            # ``_git`` uses so it never aborts on "Author identity unknown" in
            # the identity-less CI image.
            with patch.dict(os.environ, _GIT_IDENTITY_ENV):
                result = cast("str", call_command("workspace", "finalize", str(ticket.pk)))

            assert "squashed 2 commits" in result
            # The squash produced exactly one commit ahead of the base.
            base = _git(wt, "merge-base", "HEAD", "origin/main")
            assert _git(wt, "rev-list", "--count", f"{base}..HEAD") == "1"


class TestPruneSquashMergedDataLossGuard(TestCase):
    """#710 — ``_prune_squash_merged`` must honor the #706 data-loss guard.

    Uses a real on-disk git repo (no git mocks) so the guard is exercised
    against actual ``git log ... --not --remotes`` behaviour.
    """

    def _make_repo(self, tmp: Path) -> tuple[Path, Path]:
        """Build a repo whose feature branch holds genuinely-unique unpushed work.

        The branch tip tree is fed (via a mocked PR merge SHA) to the
        ``_branch_tree_matches_squash`` heuristic so it is wrongly classified as
        squash-merged — the exact path that bypasses the existing ``unsynced``
        SKIP guard in ``_prune_squash_merged``. Returns ``(work, worktree_path)``.
        """
        _remote, work = _init_repo_with_remote(tmp)

        # A genuinely-unique commit that exists on NO remote.
        _git(work, "checkout", "-q", "-b", "feature")
        (work / "unique.py").write_text("real unpushed work\n", encoding="utf-8")
        _git(work, "add", "unique.py")
        _git(work, "commit", "-q", "-m", "feat: genuinely unique unpushed work (#100)")
        _git(work, "checkout", "-q", "main")

        wt_path = tmp / "wt-feature"
        _git(work, "worktree", "add", "-q", str(wt_path), "feature")
        return work, wt_path

    def test_branch_with_unique_unpushed_work_is_not_force_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            work, wt_path = self._make_repo(tmp)
            repo = str(work)
            tip = _git(work, "rev-parse", "feature")
            wt_map = {"feature": str(wt_path)}

            # The PR-merge-SHA probe returns the branch tip itself, so
            # `_branch_tree_matches_squash` (diff --quiet tip feature) is True
            # and the existing unsynced SKIP guard is bypassed.
            with patch.object(bc_mod, "_pr_merge_commit_sha", return_value=tip):
                result = ws_cleanup_mod._prune_squash_merged(repo, "feature", wt_map, remote_ref_was_present=False)

            # DATA-LOSS ASSERTION: the unique commit must still exist.
            branches = _git(work, "branch", "--format=%(refname:short)")
            assert "feature" in branches.split(), (
                f"DATA LOSS: branch 'feature' (unpushed unique work {tip}) was deleted. "
                f"_prune_squash_merged result: {result!r}"
            )
            assert wt_path.is_dir(), "DATA LOSS: worktree directory was removed"
            assert "unique.py" in _git(work, "show", "--stat", "--format=", tip)
            assert "feature" in result, f"expected the branch named in the refusal, got: {result!r}"
            lowered = result.lower()
            assert any(token in lowered for token in ("unpushed", "no remote", "skip")), (
                f"expected a refusal/warning, got: {result!r}"
            )

    def test_genuinely_squash_merged_branch_is_still_pruned(self) -> None:
        """No regression: a branch whose work IS on origin/main is still cleaned.

        A real merged PR leaves two facts true: the source branch was pushed
        to its own remote ref (the PR existed), and the squash commit on
        ``main`` has a SHA distinct from the source-branch commit (different
        parent/message/timestamp). The original test only passed when both
        commits happened to land in the same wall-clock second and so collided
        on a single SHA — under a slow full-directory run they straddled a
        second boundary, the SHAs diverged, and the genuinely-squash-merged
        branch was wrongly classified ``unsynced`` (the #915 order-dependence).

        This models the realistic case deterministically: ``feature`` is pushed
        to ``origin`` so the #706 data-loss guard correctly sees the work IS on
        a remote, and the PR-merge-SHA probe returns the actual squash commit
        (whose tree equals the feature tip), so ``_branch_tree_matches_squash``
        classifies the distinct-SHA branch as squash-merged — independent of
        commit timestamps.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)

            _git(work, "checkout", "-q", "-b", "feature")
            (work / "f.py").write_text("work\n", encoding="utf-8")
            _git(work, "add", "f.py")
            _git(work, "commit", "-q", "-m", "feat: real work (#99)")
            # The PR existed: its source branch was pushed to a remote.
            _git(work, "push", "-q", "origin", "feature")

            # Squash-merge into main and push: the content is now on a remote.
            _git(work, "checkout", "-q", "main")
            _git(work, "merge", "-q", "--squash", "feature")
            _git(work, "commit", "-q", "-m", "feat: real work (#99)")
            squash_sha = _git(work, "rev-parse", "main")
            _git(work, "push", "-q", "origin", "main")
            _git(work, "fetch", "-q", "origin")

            wt_path = tmp / "wt-feature"
            _git(work, "worktree", "add", "-q", str(wt_path), "feature")
            repo = str(work)
            wt_map = {"feature": str(wt_path)}

            # The PR's squash commit tree equals the feature tip tree by
            # construction, so `_branch_tree_matches_squash` is True and the
            # branch is correctly classified squash-merged regardless of SHA.
            with patch.object(bc_mod, "_pr_merge_commit_sha", return_value=squash_sha):
                result = ws_cleanup_mod._prune_squash_merged(repo, "feature", wt_map, remote_ref_was_present=True)

            branches = _git(work, "branch", "--format=%(refname:short)").split()
            assert "feature" not in branches, f"squash-merged branch should be pruned, got: {result!r}"
            assert not wt_path.exists(), "worktree should be removed for a squash-merged branch"
            assert result == "Pruned squash-merged branch: feature"

    def test_unsynced_squash_merged_branch_without_linked_worktree_is_pruned(self) -> None:
        """A squash-merged branch with no linked worktree (empty wt_map) is still pruned."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "f.py").write_text("work\n", encoding="utf-8")
            _git(work, "add", "f.py")
            _git(work, "commit", "-q", "-m", "feat: real work (#99)")
            _git(work, "push", "-q", "origin", "feature")
            _git(work, "checkout", "-q", "main")
            _git(work, "merge", "-q", "--squash", "feature")
            _git(work, "commit", "-q", "-m", "feat: real work (#99)")
            squash_sha = _git(work, "rev-parse", "main")
            _git(work, "push", "-q", "origin", "main")
            _git(work, "fetch", "-q", "origin")

            # Mock the PR squash commit (tree == feature tip) so the branch is
            # classified squash-merged via the tree match, not via an accidental
            # SHA collision — hermetic regardless of commit timestamps (#915).
            with patch.object(bc_mod, "_pr_merge_commit_sha", return_value=squash_sha):
                result = ws_cleanup_mod._prune_squash_merged(str(work), "feature", {}, remote_ref_was_present=True)

            assert "feature" not in _git(work, "branch", "--format=%(refname:short)").split()
            assert result == "Pruned squash-merged branch: feature"

    def test_fails_closed_when_pushed_state_cannot_be_verified(self) -> None:
        """An inconclusive git probe must refuse deletion, not proceed (#706 fail-closed)."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "f.py").write_text("work\n", encoding="utf-8")
            _git(work, "add", "f.py")
            _git(work, "commit", "-q", "-m", "feat: real work (#99)")
            _git(work, "checkout", "-q", "main")

            wt_path = tmp / "wt-feature"
            _git(work, "worktree", "add", "-q", str(wt_path), "feature")
            tip = _git(work, "rev-parse", "feature")
            wt_map = {"feature": str(wt_path)}

            # No unsynced commits vs origin/main (skip the first guard), then
            # make the data-loss probe fail closed by pointing at a missing ref.
            with (
                patch.object(git_mod, "unsynced_commits", return_value=[]),
                patch.object(
                    git_mod,
                    "commits_absent_from_all_remotes",
                    side_effect=utils_run_mod.CommandFailedError(["git", "log"], 128, "", "fatal: bad revision"),
                ),
            ):
                result = ws_cleanup_mod._prune_squash_merged(str(work), "feature", wt_map, remote_ref_was_present=False)

            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split(), (
                f"DATA LOSS: branch deleted despite an inconclusive probe: {result!r}"
            )
            assert wt_path.is_dir(), "DATA LOSS: worktree removed despite an inconclusive probe"
            assert "SKIPPED 'feature'" in result
            assert "could not verify" in result
            assert tip  # tip captured for clarity; branch must survive


class TestPruneGoneRemoteWorktree(TestCase):
    """#1558 — clean-all must reap worktrees of squash-merged (gone-remote) branches.

    A project PR is squash-merged with branch deletion, so the merged branch's
    tip is never an ancestor of ``origin/main`` (squash creates a new SHA) and
    the remote ``origin/<branch>`` ref is gone. Uses a real on-disk git repo so
    the gone-remote classification is exercised against actual git behaviour.

    The squash-merge tree-match probe (``_pr_merge_commit_sha`` →
    ``_branch_tree_matches_squash``) needs a host CLI that is absent under test,
    so the squash commit SHA is injected (the existing data-loss-guard tests do
    the same) — hermetic regardless of commit timestamps (#915).
    """

    def _squash_merge_and_delete_remote(self, work: Path, branch: str) -> tuple[str, str]:
        """Squash-merge ``branch`` into main, push main, delete the remote ref.

        Returns ``(worktree_path, squash_sha)``. Leaves the local ``branch`` ref
        present (the worktree holds it) but ``fetch --prune`` removes
        ``refs/remotes/origin/<branch>`` — the gone-remote terminal state of a
        squash-merged + branch-deleted PR. ``squash_sha`` (the commit on main,
        whose tree equals the branch tip) is fed to ``_pr_merge_commit_sha`` so
        the tree-match classification is deterministic.
        """
        _git(work, "checkout", "-q", "-b", branch)
        (work / f"{branch}.py").write_text("work\n", encoding="utf-8")
        _git(work, "add", f"{branch}.py")
        _git(work, "commit", "-q", "-m", f"feat: {branch} (#1558)")
        _git(work, "push", "-q", "origin", branch)
        _git(work, "checkout", "-q", "main")
        _git(work, "merge", "-q", "--squash", branch)
        _git(work, "commit", "-q", "-m", f"feat: {branch} (#1558)")
        squash_sha = _git(work, "rev-parse", "main")
        _git(work, "push", "-q", "origin", "main")
        # Delete the source branch on the remote, then prune the local ref.
        _git(work, "push", "-q", "origin", "--delete", branch)
        _git(work, "fetch", "-q", "--prune", "origin")
        wt_path = work.parent / f"wt-{branch}"
        _git(work, "worktree", "add", "-q", str(wt_path), branch)
        return str(wt_path), squash_sha

    def test_gone_remote_clean_worktree_is_pruned_branch_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            wt_path, squash_sha = self._squash_merge_and_delete_remote(work, "feature")

            with patch.object(bc_mod, "_pr_merge_commit_sha", return_value=squash_sha):
                cleaned = ws_cleanup_mod.prune_branches(str(work))

            assert not Path(wt_path).is_dir(), f"gone-remote worktree should be removed, got: {cleaned!r}"
            assert any("feature" in c and "gone-remote" in c.lower() for c in cleaned), cleaned
            # The branch ref must survive — only the working tree is reaped.
            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split(), (
                f"branch ref must be kept (recoverable), got: {cleaned!r}"
            )

    def test_gone_remote_dirty_worktree_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            wt_path, squash_sha = self._squash_merge_and_delete_remote(work, "feature")
            # Uncommitted change in the worktree → must be kept.
            (Path(wt_path) / "dirty.py").write_text("local edit\n", encoding="utf-8")

            with patch.object(bc_mod, "_pr_merge_commit_sha", return_value=squash_sha):
                cleaned = ws_cleanup_mod.prune_branches(str(work))

            assert Path(wt_path).is_dir(), f"dirty worktree must be kept, got: {cleaned!r}"
            assert any("feature" in c and "uncommitted" in c.lower() for c in cleaned), cleaned
            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split()

    def test_gone_remote_worktree_with_only_regenerable_file_is_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            wt_path, squash_sha = self._squash_merge_and_delete_remote(work, "feature")
            # A regenerable env cache is not real work — the worktree is clean.
            (Path(wt_path) / ".t3-env.cache").write_text("POSTGRES_USER=x\n", encoding="utf-8")

            with patch.object(bc_mod, "_pr_merge_commit_sha", return_value=squash_sha):
                cleaned = ws_cleanup_mod.prune_branches(str(work))

            assert not Path(wt_path).is_dir(), (
                f"worktree with only regenerable files should be pruned, got: {cleaned!r}"
            )
            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split()

    def test_branch_still_on_origin_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            # Pushed branch, NOT merged, remote ref intact → open work, keep it.
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "f.py").write_text("work\n", encoding="utf-8")
            _git(work, "add", "f.py")
            _git(work, "commit", "-q", "-m", "feat: open work (#1558)")
            _git(work, "push", "-q", "origin", "feature")
            _git(work, "checkout", "-q", "main")
            _git(work, "fetch", "-q", "--prune", "origin")
            wt_path = tmp / "wt-feature"
            _git(work, "worktree", "add", "-q", str(wt_path), "feature")

            cleaned = ws_cleanup_mod.prune_branches(str(work))

            assert wt_path.is_dir(), f"branch still on origin must be kept, got: {cleaned!r}"
            assert not any("gone-remote" in c.lower() for c in cleaned), cleaned
            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split()

    def test_gone_remote_genuinely_ahead_worktree_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            wt_path, squash_sha = self._squash_merge_and_delete_remote(work, "feature")
            # A commit added in the worktree after merge that is NOT captured by
            # the squash (its tree diverges from the squash SHA) — active WIP, so
            # the worktree must be kept even though the branch ref is recoverable.
            (Path(wt_path) / "extra.py").write_text("more work\n", encoding="utf-8")
            _git(Path(wt_path), "add", "extra.py")
            _git(Path(wt_path), "commit", "-q", "-m", "feat: extra unmerged work")

            with patch.object(bc_mod, "_pr_merge_commit_sha", return_value=squash_sha):
                cleaned = ws_cleanup_mod.prune_branches(str(work))

            assert Path(wt_path).is_dir(), f"genuinely-ahead worktree must be kept, got: {cleaned!r}"
            assert any("feature" in c and "ahead of origin/main" in c for c in cleaned), cleaned
            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split()

    def test_remote_tracking_ref_exists_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            assert cleanup_mod._remote_tracking_ref_exists(str(work), "main") is True
            assert cleanup_mod._remote_tracking_ref_exists(str(work), "never-existed") is False

    def test_worktree_clean_helper_ignores_regenerable_and_missing_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            wt_path = tmp / "wt-clean"
            _git(work, "worktree", "add", "-q", str(wt_path), "-b", "side")
            assert ws_cleanup_mod._worktree_clean(str(wt_path)) is True
            (wt_path / ".t3-env.cache").write_text("X=1\n", encoding="utf-8")
            assert ws_cleanup_mod._worktree_clean(str(wt_path)) is True
            (wt_path / "real.py").write_text("real\n", encoding="utf-8")
            assert ws_cleanup_mod._worktree_clean(str(wt_path)) is False
            assert ws_cleanup_mod._worktree_clean(str(tmp / "does-not-exist")) is False

    def test_prune_gone_worktree_reports_when_removal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            wt_path, squash_sha = self._squash_merge_and_delete_remote(work, "feature")

            with (
                patch.object(bc_mod, "_pr_merge_commit_sha", return_value=squash_sha),
                patch.object(git_mod, "worktree_remove", return_value=False),
            ):
                result = ws_cleanup_mod._prune_gone_worktree(str(work), "feature", wt_path)

            assert "SKIPPED 'feature'" in result
            assert "git worktree remove failed" in result
            # Removal failed → the working tree and branch ref both survive.
            assert Path(wt_path).is_dir()
            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split()


class TestRefuseIfUnpushedAncestryFallback(TestCase):
    """#2205 — ``_refuse_if_unpushed`` must pass when HEAD is ancestor of origin/main.

    When a branch's remote tracking ref is deleted (squash-merge + branch deletion
    + ``fetch --prune``), ``commits_absent_from_all_remotes`` returns non-empty:
    the tracking ref is gone, and squash creates a distinct SHA on main so the
    branch commits are not reachable from any remaining ``refs/remotes/*``.

    The old code returned a refusal message ("SKIPPED … on NO remote"). After the
    fix an ancestry check is applied: if every branch commit is reachable from
    ``origin/<default>`` the work is already on main and deletion is safe —
    ``_refuse_if_unpushed`` must return ``""`` (allow deletion).
    """

    def _squash_merge_remote_delete(self, tmp: Path, branch: str) -> tuple[Path, Path]:
        """Return (work_repo, remote).  branch squash-merged → main, source ref deleted FORGE-side.

        The source ref is deleted on the bare remote directly (``update-ref -d``),
        modelling a forge squash-merge: the local clone keeps a STALE
        ``origin/<branch>`` tracking ref until a later fetch/prune — the
        forge-CLI-free squash-merge signal the caller samples before the prune.
        """
        remote, work = _init_repo_with_remote(tmp)
        _git(work, "checkout", "-q", "-b", branch)
        (work / f"{branch}.py").write_text("work\n", encoding="utf-8")
        _git(work, "add", f"{branch}.py")
        _git(work, "commit", "-q", "-m", f"feat: {branch}")
        _git(work, "push", "-q", "origin", branch)
        _git(work, "checkout", "-q", "main")
        _git(work, "merge", "-q", "--squash", branch)
        _git(work, "commit", "-q", "-m", f"feat: {branch} (#2205)")
        _git(work, "push", "-q", "origin", "main")
        _git(remote, "update-ref", "-d", f"refs/heads/{branch}")
        return work, remote

    def test_squash_merged_remote_deleted_branch_is_allowed(self) -> None:
        """#2205 repro: ``_refuse_if_unpushed`` must not block a squash-merged branch."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            work, _remote = self._squash_merge_remote_delete(tmp, "feature")
            # The caller samples the stale tracking ref before its fetch/prune.
            present = cleanup_mod._remote_tracking_ref_exists(str(work), "feature")
            _git(work, "fetch", "-q", "--prune", "origin")

            result = ws_cleanup_mod._refuse_if_unpushed(str(work), "feature", remote_ref_was_present=present)

        assert present is True, "Forge-side delete must leave a stale tracking ref to sample"
        assert result == "", f"Expected '' (safe to delete), got refusal: {result!r}"

    def test_genuinely_unpushed_branch_is_still_refused(self) -> None:
        """No regression: a branch with commits on NO remote at all must still be refused."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "f.py").write_text("never pushed\n", encoding="utf-8")
            _git(work, "add", "f.py")
            _git(work, "commit", "-q", "-m", "feat: genuinely unpushed")

            result = ws_cleanup_mod._refuse_if_unpushed(str(work), "feature", remote_ref_was_present=False)

        assert result != "", "Expected a refusal message for genuinely unpushed work"
        assert "feature" in result

    def _local_only_tree_matches_main(self, tmp: Path, branch: str) -> Path:
        """Local-only branch with NEVER-pushed commits whose final tree == origin/main.

        Mirrors the reviewer's data-loss repro: ``add feat`` then ``revert - back
        to main tree``. The branch is never pushed to any remote, so its commits
        live nowhere but locally, yet ``git diff --quiet branch origin/main`` exits
        0 because the cumulative tree is identical to ``origin/main``.
        """
        _remote, work = _init_repo_with_remote(tmp)
        _git(work, "checkout", "-q", "-b", branch)
        (work / "feat.py").write_text("genuinely local work\n", encoding="utf-8")
        _git(work, "add", "feat.py")
        _git(work, "commit", "-q", "-m", "add feat")
        _git(work, "rm", "-q", "feat.py")
        _git(work, "commit", "-q", "-m", "revert - back to main tree")
        return work

    def test_local_only_commits_with_matching_tree_are_refused(self) -> None:
        """Finding 1 (data loss): tree-equality alone must NOT allow deletion.

        A branch whose commits were never pushed anywhere, but whose final tree
        coincidentally equals ``origin/main`` (work added then reverted), passes
        ``git diff --quiet`` yet has NO positive merged-evidence. Deleting it
        destroys the only copy of those commits. Without a forge merged signal the
        guard must KEEP the branch.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            work = self._local_only_tree_matches_main(tmp, "feature")

            with patch.object(cleanup_mod, "_branch_pr_is_merged", return_value=False):
                result = ws_cleanup_mod._refuse_if_unpushed(str(work), "feature", remote_ref_was_present=False)

        assert result != "", "Expected a refusal: local-only commits whose tree matches main must be kept"
        assert "feature" in result

    def test_local_only_matching_tree_pruned_only_with_merged_evidence(self) -> None:
        """Tree-equality IS accepted once the forge confirms the PR merged.

        The secondary confirmation (tree matches) is gated behind positive
        merged-evidence: with the forge reporting the PR merged, the same
        tree-matching branch is safe to delete.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            work = self._local_only_tree_matches_main(tmp, "feature")

            with patch.object(cleanup_mod, "_branch_pr_is_merged", return_value=True):
                result = ws_cleanup_mod._refuse_if_unpushed(str(work), "feature", remote_ref_was_present=False)

        assert result == "", f"Expected '' (merged-evidence + tree match → safe), got: {result!r}"


def _squash_merge_into_main(tmp: Path, *, subject: str) -> tuple[Path, str]:
    """Build a repo whose ``feature`` branch is squash-merged into main under ``subject``.

    The feature branch is pushed to its own remote ref (the PR existed, so the
    #706 data-loss guard sees the work on a remote), then squash-merged into a
    pushed ``main`` under a *different* subject. ``git diff origin/main...feature
    --stat`` is therefore empty — the ``is_squash_merged`` empty-diff fallback
    fires even though no subject matches. Returns ``(work_repo, feature_tip)``.
    """
    _remote, work = _init_repo_with_remote(tmp)
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "f.py").write_text("shipped work\n", encoding="utf-8")
    _git(work, "add", "f.py")
    _git(work, "commit", "-q", "-m", "wip: scratch subject that will not match")
    _git(work, "push", "-q", "origin", "feature")
    tip = _git(work, "rev-parse", "feature")
    _git(work, "checkout", "-q", "main")
    _git(work, "merge", "-q", "--squash", "feature")
    _git(work, "commit", "-q", "-m", subject)
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")
    return work, tip


class TestRemoveEmptyTicketDirs(TestCase):
    """#1940 gap (b): a ticket dir holding only empty repo subdirs is removed.

    A multi-repo ticket dir (``ac/1234/`` with empty ``backend/`` + ``frontend/``)
    has children, so the old single-level ``not any(iterdir())`` check left it
    behind. The recursive remover prunes the empty leaves then the now-empty
    ticket dir.
    """

    def _remove(self, workspace: Path) -> list[str]:
        return ws_cleanup_mod.WorktreeReaper(workspace).remove_empty_ticket_dirs()

    def test_ticket_dir_with_only_empty_repo_subdirs_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            workspace = Path(tmp_s)
            ticket_dir = workspace / "ac-1234"
            (ticket_dir / "backend").mkdir(parents=True)
            (ticket_dir / "frontend").mkdir(parents=True)

            removed = self._remove(workspace)

            assert not ticket_dir.exists(), f"empty multi-repo ticket dir should be removed; got: {removed!r}"
            assert any("ac-1234" in r for r in removed)

    def test_ticket_dir_with_real_files_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            workspace = Path(tmp_s)
            ticket_dir = workspace / "ac-5678"
            (ticket_dir / "backend").mkdir(parents=True)
            (ticket_dir / "backend" / "real.py").write_text("x\n", encoding="utf-8")

            removed = self._remove(workspace)

            assert ticket_dir.exists(), "ticket dir with real content must be kept"
            assert not any("ac-5678" in r for r in removed)

    def test_top_level_file_is_left_untouched(self) -> None:
        """A loose file at the workspace root is not a ticket dir — skip it."""
        with tempfile.TemporaryDirectory() as tmp_s:
            workspace = Path(tmp_s)
            loose = workspace / "notes.txt"
            loose.write_text("x\n", encoding="utf-8")

            removed = self._remove(workspace)

            assert loose.exists()
            assert removed == []


def _make_squash_merged_worktree(tmp: Path, *, overlay: str = "test", ticket_number: str = "200") -> Worktree:
    """Build a real-git squash-merged worktree row whose work is already on origin/main.

    ``feature`` is pushed, squash-merged into ``main`` and pushed, so the
    #706/#835 data-loss guards see the work on a remote and clean-all's
    ``is_squash_merged`` empty-diff fallback classifies it as merged — the
    safe-to-reap path. The row carries ``clone_path``/``worktree_path`` extras so
    ``cleanup_worktree`` resolves the real on-disk worktree.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    _remote, work = _init_repo_with_remote(tmp)
    _git(work, "checkout", "-q", "-b", "feature")
    (work / "f.py").write_text("work\n", encoding="utf-8")
    _git(work, "add", "f.py")
    _git(work, "commit", "-q", "-m", f"feat: shipped work (#{ticket_number})")
    _git(work, "push", "-q", "origin", "feature")
    _git(work, "checkout", "-q", "main")
    _git(work, "merge", "-q", "--squash", "feature")
    # A distinct subject keeps the squash commit's SHA divergent from feature's
    # tip even when both land in the same wall-clock second (#915 collision),
    # so feature is deterministically NOT an ancestor of origin/main.
    _git(work, "commit", "-q", "-m", f"feat: shipped work via squash (#{ticket_number})")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "fetch", "-q", "origin")

    wt_path = tmp / "wt-feature"
    _git(work, "worktree", "add", "-q", str(wt_path), "feature")

    ticket = Ticket.objects.create(overlay=overlay, issue_url=f"https://example.com/issues/{ticket_number}")
    return Worktree.objects.create(
        overlay=overlay,
        ticket=ticket,
        repo_path="backend",
        branch="feature",
        db_name=f"wt_test_{ticket_number}",
        state=Worktree.State.PROVISIONED,
        extra={"clone_path": str(work), "worktree_path": str(wt_path)},
    )


@_no_prune
@_no_stash
@_no_orphan_dbs
@_no_orphan_isolated_roots
@_no_orphan_docker
@_no_dslr_prune
@_no_liveness
@_patch_overlays(FULL_OVERLAY)
@override_settings(**SETTINGS)
@_no_orphan_raw
class TestCleanAllReapsAndSurvivesForeignOverlay(TestCase):
    """clean-all reaps a SAFE merged worktree fully and never crashes on a foreign overlay.

    Uses a real on-disk git repo (``_init_repo_with_remote``) so the data-loss
    guards and squash-merge classification run against actual git, with only the
    docker-down and DB-drop side effects stubbed.
    """

    def _run_clean_all(self, workspace: Path) -> tuple[list[str], MagicMock, list[str]]:
        """Run clean-all against real git, stubbing only docker-down and the DB drop.

        The git layer is NEVER mocked — the #706/#835 data-loss guards must run
        against actual ``git log ... --not --remotes`` behaviour. Only the two
        external side effects (``docker compose down`` and ``dropdb``) are
        intercepted, the DB drop recorded into the returned ``dropped`` list.
        """
        dropped: list[str] = []
        with (
            patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
            patch.object(provision_mod, "_workspace_dir", return_value=workspace),
            patch.object(cleanup_mod, "load_config") as mock_config,
            patch("teatree.core.runners.worktree_start.docker_compose_down") as mock_docker_down,
            patch.object(cleanup_mod, "drop_db", side_effect=lambda name, **_kw: dropped.append(name)),
        ):
            mock_config.return_value.user.workspace_dir = workspace
            cleaned = cast("list[str]", call_command("workspace", "clean-all"))
        return cleaned, mock_docker_down, dropped

    def test_reaps_merged_worktree_fully(self) -> None:
        """A squash-merged worktree loses its worktree dir, branch, DB and docker stack."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            wt = _make_squash_merged_worktree(tmp)
            work = Path(wt.extra["clone_path"])
            wt_path = Path(wt.extra["worktree_path"])

            cleaned, mock_docker_down, dropped = self._run_clean_all(tmp)

            assert Worktree.objects.count() == 0, f"row should be gone, got: {cleaned!r}"
            assert not wt_path.exists(), "git worktree dir should be removed"
            assert "feature" not in _git(work, "branch", "--format=%(refname:short)").split()
            mock_docker_down.assert_called_once_with("backend-wt200", remove_volumes=True)
            assert "wt_test_200" in dropped, f"dropdb not invoked: {dropped!r}"
            assert any("Cleaned" in c for c in cleaned)

    def test_keeps_unmerged_worktree_with_unique_work(self) -> None:
        """Even on a DONE ticket, genuinely-unique unpushed work is kept — the analyze gate.

        The done (MERGED) ticket clears the necessary gate, so the per-change
        analyze-before-wipe step (the #706 data-loss guard, hoisted to primary) is
        the only thing keeping the unique unpushed commit: it is not provably on
        ``origin/main``, so the worktree is kept and reported for salvage.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "unique.py").write_text("real unpushed work\n", encoding="utf-8")
            _git(work, "add", "unique.py")
            _git(work, "commit", "-q", "-m", "feat: genuinely unique unpushed work (#201)")
            _git(work, "checkout", "-q", "main")
            wt_path = tmp / "wt-feature"
            _git(work, "worktree", "add", "-q", str(wt_path), "feature")

            ticket = Ticket.objects.create(
                overlay="test", issue_url="https://example.com/issues/201", state=Ticket.State.MERGED
            )
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                state=Worktree.State.PROVISIONED,
                extra={"clone_path": str(work), "worktree_path": str(wt_path)},
            )

            cleaned, _docker, _dropped = self._run_clean_all(tmp)

            assert Worktree.objects.count() == 1, "row with unpushed unique work must survive"
            assert wt_path.is_dir(), "DATA LOSS: worktree dir was removed"
            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split()
            assert any("feature" in c and "KEPT" in c for c in cleaned), f"expected a keep line, got: {cleaned!r}"

    def test_foreign_overlay_merged_row_is_reaped_overlay_free_not_skipped(self) -> None:
        """A merged row whose overlay is unregistered is REAPED overlay-free, not skipped.

        Skipping foreign/unregistered-overlay rows (the old #2472 behaviour) is
        exactly what left hundreds of stale worktrees + their docker/DB behind:
        a sibling product overlay's row reaped from a clone where only the teatree
        overlay is installed could never be cleaned. ``cleanup_worktree`` now
        resolves the overlay tolerantly and runs
        the overlay-agnostic teardown, so a *merged* foreign-overlay worktree is
        actually reaped — git worktree + branch removed, row deleted — while the
        #706/#835 data-loss guards (all git-based) still protect unmerged work.
        The crash-safety #2472 added is preserved: the run never aborts mid-sweep.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            good = _make_squash_merged_worktree(tmp / "good", overlay="test", ticket_number="202")

            foreign_tmp = tmp / "foreign"
            foreign_tmp.mkdir()
            foreign = _make_squash_merged_worktree(foreign_tmp, overlay="overlay-uninstalled", ticket_number="203")
            foreign_path = Path(foreign.extra["worktree_path"])

            cleaned, _docker, _dropped = self._run_clean_all(tmp)

            assert not Worktree.objects.filter(pk=good.pk).exists(), "registered merged row should be reaped"
            assert not Worktree.objects.filter(pk=foreign.pk).exists(), (
                "foreign-overlay MERGED row must now be reaped overlay-free, not skipped"
            )
            assert not foreign_path.is_dir(), "foreign worktree dir must be removed"
            assert sum("Cleaned" in c for c in cleaned) == 2, (
                f"both the registered and foreign merged rows should be cleaned, got: {cleaned!r}"
            )

    def test_foreign_overlay_unmerged_row_is_kept_by_data_loss_guard(self) -> None:
        """The overlay-free reap still keeps an UNMERGED foreign-overlay worktree.

        The symmetric guarantee to the merged-reap above: reaping foreign-overlay
        rows must never bypass the #706 data-loss guard. A foreign-overlay
        worktree carrying genuinely-unique unpushed work is KEPT (its dir and row
        survive) — the guard runs on git state alone, so no overlay is required to
        protect the work.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "unique.py").write_text("real unpushed work\n", encoding="utf-8")
            _git(work, "add", "unique.py")
            _git(work, "commit", "-q", "-m", "feat: unique unpushed work on a foreign overlay (#204)")
            _git(work, "checkout", "-q", "main")
            wt_path = tmp / "wt-feature"
            _git(work, "worktree", "add", "-q", str(wt_path), "feature")

            ticket = Ticket.objects.create(
                overlay="overlay-uninstalled",
                issue_url="https://example.com/issues/204",
                state=Ticket.State.MERGED,
            )
            wt = Worktree.objects.create(
                overlay="overlay-uninstalled",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                state=Worktree.State.PROVISIONED,
                extra={"clone_path": str(work), "worktree_path": str(wt_path)},
            )

            # The ticket is DONE (MERGED), so the necessary gate is cleared; the
            # per-change analyze step (the #706 guard) is the only thing that may
            # keep the unique unpushed work — and no overlay is required to run it.
            cleaned, _docker, _dropped = self._run_clean_all(tmp)

            assert Worktree.objects.filter(pk=wt.pk).exists(), "unmerged foreign-overlay row must be kept"
            assert wt_path.is_dir(), "DATA LOSS: foreign-overlay worktree dir with unique work was removed"
            assert any("feature" in c and "KEPT" in c for c in cleaned), f"expected a keep line, got: {cleaned!r}"

    def test_unclassifiable_sibling_repo_is_skipped_not_crashed(self) -> None:
        """A row whose sibling repo cannot be classified is kept, not fatal.

        A corrupt/origin-less clone makes ``git.default_branch`` /
        ``is_squash_merged`` raise; the done-signal fails safe to NOT done, so the
        reaper keeps that one row with a reported reason rather than aborting the run.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            broken = tmp / "broken"
            broken.mkdir()
            work = broken / "work"
            subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)  # noqa: S607
            _git(work, "commit", "-q", "--allow-empty", "-m", "base")
            wt_path = broken / "wt-feature"
            _git(work, "checkout", "-q", "-b", "feature")
            _git(work, "worktree", "add", "-q", str(wt_path), "main")
            _git(work, "checkout", "-q", "feature")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/204")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                state=Worktree.State.PROVISIONED,
                extra={"clone_path": str(work), "worktree_path": str(wt_path)},
            )

            cleaned, _docker, _dropped = self._run_clean_all(tmp)

            assert Worktree.objects.filter(pk=wt.pk).exists(), "unclassifiable row must be kept, not crashed on"
            assert any("feature" in c and "KEPT" in c for c in cleaned), (
                f"expected a keep line for the unclassifiable row, got: {cleaned!r}"
            )


@_no_prune
@_no_stash
@_no_orphan_dbs
@_no_orphan_isolated_roots
@_no_orphan_docker
@_no_dslr_prune
@_patch_overlays(FULL_OVERLAY)
@override_settings(**SETTINGS)
@_no_orphan_raw
class TestCleanAllKeepsBusyWorktree(TestCase):
    """clean-all KEEPS a worktree under live work, never reaping it mid-task (#2243/#2773).

    The reconciliation home for #2773's end-to-end busy-keep guards: ported off the
    ``@_no_liveness``-neutralised reap-fully class so the REAL
    :func:`teatree.core.cleanup_liveness.worktree_liveness` predicate runs. The
    ad-hoc ``clean-all`` sweep funnels through :func:`reap_done_worktree` with
    ``fsm_terminal`` OFF, so a busy ticket (live session / claimed task) flips a
    squash-merged or CREATED row from would-reap to an ``ACTIVE … skipping`` KEEP —
    the data-loss discipline #2773 widened and #2763 enforces at the reaper's
    liveness pre-gate. ``test_reaps_merged_worktree_fully`` proves the same
    squash-merged row IS reaped when idle, so each KEEP here is a non-vacuous,
    red-first guard.
    """

    def _run_clean_all(self, workspace: Path) -> tuple[list[str], MagicMock, list[str]]:
        """Run clean-all against real git, stubbing only docker-down and the DB drop.

        Mirrors ``TestCleanAllReapsAndSurvivesForeignOverlay._run_clean_all`` but
        WITHOUT the liveness neutralisation — the live-work KEEP is exactly what is
        under test here. The git layer is never mocked.
        """
        dropped: list[str] = []
        with (
            patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
            patch.object(provision_mod, "_workspace_dir", return_value=workspace),
            patch.object(cleanup_mod, "load_config") as mock_config,
            patch("teatree.core.runners.worktree_start.docker_compose_down") as mock_docker_down,
            patch.object(cleanup_mod, "drop_db", side_effect=lambda name, **_kw: dropped.append(name)),
        ):
            mock_config.return_value.user.workspace_dir = workspace
            cleaned = cast("list[str]", call_command("workspace", "clean-all"))
        return cleaned, mock_docker_down, dropped

    def test_keeps_busy_squash_merged_worktree(self) -> None:
        """A squash-merged worktree whose ticket has live work is KEPT, not reaped (#2243).

        ``test_reaps_merged_worktree_fully`` proves the same row IS reaped when
        idle, so a live :class:`Session` flipping it to KEEP is a non-vacuous
        red-first guard: clean-all must never tear down a squash-merged
        follow-up worktree an agent is mid-task in.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            wt = _make_squash_merged_worktree(tmp)
            wt_path = Path(wt.extra["worktree_path"])
            Session.objects.create(ticket=wt.ticket, overlay="test")  # live: ended_at is null

            cleaned, _docker, _dropped = self._run_clean_all(tmp)

            assert Worktree.objects.filter(pk=wt.pk).exists(), f"DATA LOSS: busy merged row reaped; got: {cleaned!r}"
            assert wt_path.is_dir(), "DATA LOSS: busy worktree dir removed"
            assert any("ACTIVE" in c and "skipping" in c for c in cleaned), (
                f"expected a live-work skip line, got: {cleaned!r}"
            )

    def test_keeps_busy_created_worktree(self) -> None:
        """The CREATED-state row keeps a worktree whose ticket has live work (#2243).

        The liveness pre-gate in :func:`reap_done_worktree` fires before
        done-detection, so a busy CREATED row survives clean-all with an
        ``ACTIVE … skipping`` line — never handed to teardown. The safe-reap of an
        IDLE worktree is covered by ``test_reaps_merged_worktree_fully`` and the
        cleanup-level ``TestCleanupWorktreeLivenessGuard.test_idle_worktree_is_torn_down``.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            workspace = tmp / "workspace"
            workspace.mkdir()

            busy_ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/2243a")
            busy = Worktree.objects.create(
                overlay="test",
                ticket=busy_ticket,
                repo_path="backend",
                branch="busy",
                state=Worktree.State.CREATED,
                extra={"worktree_path": str(workspace / "busy" / "backend")},
            )
            session = Session.objects.create(ticket=busy_ticket, overlay="test")
            session.ended_at = timezone.now()
            session.save(update_fields=["ended_at"])
            Task.objects.create(ticket=busy_ticket, session=session, status=Task.Status.CLAIMED)

            with patch.object(workspace_mod, "_workspace_dir", return_value=workspace):
                cleaned = cast("list[str]", call_command("workspace", "clean-all"))

            assert Worktree.objects.filter(pk=busy.pk).exists(), "DATA LOSS: busy CREATED worktree reaped"
            assert any("ACTIVE" in c and "skipping" in c for c in cleaned), (
                f"expected a live-work skip line, got: {cleaned!r}"
            )


class TestIsSquashMergedRealGit(TestCase):
    """is_squash_merged detects a real squash-merge via patch-id (git cherry).

    A squash-merge rewrites the source commits into one new SHA on the default
    branch, so the branch is NOT an ancestor of origin/<default> and the old
    three-dot-diff / is-ancestor test missed it. These exercise the forge-CLI-free
    fallback against real git, with gh/glab forced absent so only the git path runs.
    """

    @staticmethod
    def _no_host_cli() -> AbstractContextManager[object]:
        # Forge absent: the corroborating forge probe yields nothing, so only the
        # deterministic git content layers (cherry / synthetic-squash / --merged) decide.
        return patch.object(bc_mod, "probe_host_cli", return_value="")

    def test_squash_merged_branch_is_detected_despite_non_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            wt = _make_squash_merged_worktree(tmp, ticket_number="300")
            work = Path(wt.extra["clone_path"])
            is_ancestor = subprocess.run(
                ["git", "-C", str(work), "merge-base", "--is-ancestor", "feature", "origin/main"],  # noqa: S607
                check=False,
            ).returncode
            assert is_ancestor != 0, "precondition: squash-merged branch is NOT an ancestor of origin/main"
            with self._no_host_cli():
                assert bc_mod.is_squash_merged(str(work), "feature", "main") is True

    def test_divergent_branch_is_not_detected_as_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "unique.py").write_text("genuinely unmerged\n", encoding="utf-8")
            _git(work, "add", "unique.py")
            _git(work, "commit", "-q", "-m", "feat: never merged (#301)")
            _git(work, "checkout", "-q", "main")
            with self._no_host_cli():
                assert bc_mod.is_squash_merged(str(work), "feature", "main") is False


@_no_prune
@_no_stash
@_no_orphan_dbs
@_no_orphan_isolated_roots
@_no_orphan_docker
@_no_orphan_raw
@_no_dslr_prune
@_no_liveness
@_patch_overlays(FULL_OVERLAY)
@override_settings(**SETTINGS)
class TestCleanAllUnattendedReapMatrix(TestCase):
    """End-to-end ``call_command('workspace','clean-all')`` reap decisions (#1830, #2361).

    Each case runs the whole command unattended (no ``--interactive``) against a
    real on-disk git repo, mocking only the unstoppable forge CLI (``gh``/``glab``
    via ``probe_host_cli`` / ``_branch_pr_is_merged``) and the docker-down / dropdb
    side effects — never the git layer, so the deterministic squash signals and
    data-loss guards run for real. Asserts the reap/keep decision and no prompt.
    """

    def _run(self, workspace: Path, *, forge: "subprocess.CompletedProcess[str] | None") -> list[str]:
        dropped: list[str] = []

        def _input_must_not_be_called(*_a: object, **_k: object) -> str:
            msg = "unattended clean-all blocked on stdin (#2361)"
            raise AssertionError(msg)

        forge_merged = forge is not None
        with (
            patch.object(workspace_mod, "_workspace_dir", return_value=workspace),
            patch.object(provision_mod, "_workspace_dir", return_value=workspace),
            patch.object(cleanup_mod, "load_config") as mock_config,
            # The forge is corroborating-only now: stub both its probe (so no real
            # gh/glab subprocess runs) and the merged report. It never alone reaps.
            patch.object(bc_mod, "probe_host_cli", return_value="42" if forge_merged else ""),
            patch.object(bc_mod, "_branch_pr_is_merged", return_value=forge_merged),
            patch.object(cleanup_mod, "_branch_pr_is_merged", return_value=forge_merged),
            patch("teatree.core.runners.worktree_start.docker_compose_down"),
            patch.object(cleanup_mod, "drop_db", side_effect=lambda name, **_kw: dropped.append(name)),
            patch("builtins.input", side_effect=_input_must_not_be_called),
        ):
            mock_config.return_value.user.workspace_dir = workspace
            return cast("list[str]", call_command("workspace", "clean-all"))

    def _row(self, work: Path, wt_path: Path, *, branch: str = "feature", number: str = "1830") -> Worktree:
        ticket = Ticket.objects.create(overlay="test", issue_url=f"https://example.com/issues/{number}")
        return Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="backend",
            branch=branch,
            state=Worktree.State.PROVISIONED,
            extra={"clone_path": str(work), "worktree_path": str(wt_path)},
        )

    def test_patch_id_merged_branch_is_reaped(self) -> None:
        """A squash-merged branch (non-ancestor) is reaped via the git-cherry patch-id signal.

        Forge forced absent (``forge=None``), so only the deterministic
        ``_branch_captured_upstream`` (``git cherry``) path can classify it merged.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            wt = _make_squash_merged_worktree(tmp, ticket_number="1830")
            work = Path(wt.extra["clone_path"])
            wt_path = Path(wt.extra["worktree_path"])

            cleaned = self._run(tmp, forge=None)

            assert not Worktree.objects.filter(pk=wt.pk).exists(), f"patch-id-merged row should be reaped: {cleaned!r}"
            assert not wt_path.exists()
            assert "feature" not in _git(work, "branch", "--format=%(refname:short)").split()

    def test_forge_merged_but_tip_not_on_target_is_kept_not_reaped(self) -> None:
        """A forge-merged branch whose CURRENT tip is NOT on origin/main is KEPT (#2763).

        The branch is pushed and genuinely ahead of origin/main (no empty diff, no
        cherry-equivalence, no squash on main), so the forge MR/PR record is the
        ONLY merged signal. Under the canonical layered detection the forge signal
        is corroborating-only and NEVER alone authorises deletion: the row is kept
        for salvage to a fresh PR, never reaped on the stale merged signal.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "shipped.py").write_text("merged via forge\n", encoding="utf-8")
            _git(work, "add", "shipped.py")
            _git(work, "commit", "-q", "-m", "feat: shipped, forge-only signal")
            _git(work, "push", "-q", "origin", "feature")
            _git(work, "checkout", "-q", "main")
            wt_path = tmp / "wt-feature"
            _git(work, "worktree", "add", "-q", str(wt_path), "feature")
            wt = self._row(work, wt_path, number="1830b")

            forge_merged = subprocess.CompletedProcess([], 0, '[{"number": 42}]', "")
            cleaned = self._run(tmp, forge=forge_merged)

            assert Worktree.objects.filter(pk=wt.pk).exists(), (
                f"forge-merged alone must NOT reap a tip not on origin/main (#2763): {cleaned!r}"
            )
            assert wt_path.is_dir()

    def test_dirty_live_worktree_is_kept(self) -> None:
        """#2243 / #835: a worktree with uncommitted changes is KEPT, never reaped.

        Even when the forge reports the branch merged, a dirty working tree means
        live in-progress work — the data-loss guard keeps it. This is the
        deterministic live-worktree protection: never delete a dir in use.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            wt = _make_squash_merged_worktree(tmp, ticket_number="2243")
            work = Path(wt.extra["clone_path"])
            wt_path = Path(wt.extra["worktree_path"])
            (wt_path / "in_progress.py").write_text("agent mid-task\n", encoding="utf-8")
            _git(wt_path, "add", "in_progress.py")

            forge_merged = subprocess.CompletedProcess([], 0, '[{"number": 2243}]', "")
            cleaned = self._run(tmp, forge=forge_merged)

            assert Worktree.objects.filter(pk=wt.pk).exists(), f"DATA LOSS: dirty live worktree reaped: {cleaned!r}"
            assert wt_path.is_dir(), "dirty live worktree dir must survive"
            assert "feature" in _git(work, "branch", "--format=%(refname:short)").split()

    def test_ambiguous_unmerged_branch_is_kept(self) -> None:
        """A branch with unique unpushed work and no merged signal is kept with a warning.

        Forge absent + genuinely-ahead commits on no remote → uncertain → KEEP,
        never delete on a guess.
        """
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            _remote, work = _init_repo_with_remote(tmp)
            _git(work, "checkout", "-q", "-b", "feature")
            (work / "unique.py").write_text("real unpushed work\n", encoding="utf-8")
            _git(work, "add", "unique.py")
            _git(work, "commit", "-q", "-m", "feat: genuinely unique unpushed work")
            _git(work, "checkout", "-q", "main")
            wt_path = tmp / "wt-feature"
            _git(work, "worktree", "add", "-q", str(wt_path), "feature")
            wt = self._row(work, wt_path, number="1830c")

            cleaned = self._run(tmp, forge=None)

            assert Worktree.objects.filter(pk=wt.pk).exists(), f"ambiguous row must be kept: {cleaned!r}"
            assert wt_path.is_dir()
