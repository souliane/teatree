"""``workspace ticket``'s forge read happens before the control-DB write lock is taken.

Every ``atomic()`` opens ``BEGIN IMMEDIATE``, so a forge read inside one holds SQLite's
write lock for as long as the forge takes, up to its 60 s timeout, while every other
writer waits at most the 30 s busy timeout.
"""

import os
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.utils.module_loading import import_string

import teatree.core.overlay_loader as overlay_loader_mod
import teatree.utils.run as utils_run_mod
from teatree.core.management.commands._workspace import ticket_intake as ticket_intake_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.runners.base import RunnerResult
from tests._git_repo import make_git_repo, run_git
from tests.teatree_core.conftest import CommandOverlay
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


class TestTheIssueTitleIsReadOutsideTheWriteTransaction(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(
            patch.object(utils_run_mod.subprocess, "run", return_value=MagicMock(returncode=0, stdout="", stderr=""))
        )
        # ``cut_start_point`` reads origin through Popen, which the ``subprocess.run`` mock never reaches.
        self.enterContext(patch("teatree.core.runners.provision.git.cut_start_point", return_value="origin/main"))
        workspace = Path(os.environ["HOME"]) / "workspace"
        for repo in ("backend", "frontend"):
            (workspace / repo / ".git").mkdir(parents=True, exist_ok=True)

    @override_settings(**SETTINGS)
    def test_the_title_fetch_runs_at_the_callers_transaction_depth(self) -> None:
        depth_at_fetch: list[int] = []

        def _title(_url: str) -> str:
            depth_at_fetch.append(len(connection.atomic_blocks))
            return "Fix Login Flow"

        overlay = import_string(FULL_OVERLAY)()
        overlay.get_issue_title = _title
        caller_depth = len(connection.atomic_blocks)

        with patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": overlay}):
            ticket_id = cast("int", call_command("workspace", "ticket", "https://example.com/issues/4913"))

        assert depth_at_fetch == [caller_depth]
        assert Ticket.objects.get(pk=ticket_id).extra["description"] == "Fix Login Flow"


class TestTheSummaryReportsTheCheckout(TestCase):
    """The intake summary names the branch each worktree is on, and a refusal names git's error (#4967)."""

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path: Path) -> None:
        self.tmp = tmp_path

    def _ticket(self, branch: str, *, worktree_path: str = "", recorded_branch: str = "") -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test", issue_url="https://example.com/issues/4967", repos=["repo-a"], extra={"branch": branch}
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="repo-a",
            branch=recorded_branch or branch,
            extra={"worktree_path": worktree_path} if worktree_path else {},
        )
        return ticket

    def _finalize_after(self, ticket: Ticket, result: RunnerResult) -> tuple[str, int]:
        out: list[str] = []
        provisioner = MagicMock()
        provisioner.run.return_value = result
        with patch.object(ticket_intake_mod, "WorktreeProvisioner", return_value=provisioner):
            rc = ticket_intake_mod.finalize_ticket_provision(out.append, lambda _s: None, ticket, None)
        return "\n".join(out), rc

    def test_names_the_branch_actually_checked_out(self) -> None:
        clone = make_git_repo(self.tmp / "repo-a")
        checkout = self.tmp / "requested-y" / "repo-a"
        run_git(clone, "worktree", "add", "-q", "-b", "actual-x", str(checkout))
        ticket = self._ticket("requested-y", worktree_path=str(checkout))

        summary, rc = self._finalize_after(ticket, RunnerResult(ok=True, detail="provisioned 1 worktree(s)"))

        assert rc == ticket.pk
        assert "on actual-x (ticket records requested-y)" in summary
        assert "Branch: requested-y" not in summary

    def test_a_row_with_no_checkout_never_reads_the_current_directory(self) -> None:
        ticket = self._ticket("4967-no-path")

        summary, _rc = self._finalize_after(ticket, RunnerResult(ok=False, detail="failed to create worktrees for: x"))

        assert "repo-a: worktree #" in summary
        assert "no readable checkout" in summary

    def test_a_refused_provision_reports_gits_fetch_error(self) -> None:
        origin = make_git_repo(self.tmp / "origin" / "repo-a.git", bare=True)
        clone = make_git_repo(self.tmp / "workspace" / "repo-a")
        run_git(clone, "remote", "add", "origin", str(origin))
        run_git(clone, "push", "-q", "origin", "main")
        run_git(clone, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        ticket = Ticket.objects.create(
            overlay="test", issue_url="https://example.com/issues/4968", repos=["repo-a"], extra={"branch": "4968-x"}
        )
        errs: list[str] = []
        with (
            patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": CommandOverlay()}),
            patch("teatree.core.runners.provision.clone_root", return_value=self.tmp / "workspace"),
            patch("teatree.core.runners.provision.worktree_root", return_value=self.tmp / "worktrees"),
            patch.object(ticket_intake_mod, "worktree_root", return_value=self.tmp / "worktrees"),
        ):
            rc = ticket_intake_mod.finalize_ticket_provision(lambda _s: None, errs.append, ticket, None)

        assert rc == 0
        (reported,) = errs
        assert reported.startswith("  Provisioning failed:")
        assert "does not appear to be a git repository" in reported
