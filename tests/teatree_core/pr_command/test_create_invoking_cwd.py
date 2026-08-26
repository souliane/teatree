"""``pr create`` takes its bearings from the DECLARED invocation cwd.

Under the containerized ``t3`` the process cwd is the image WORKDIR
(``/home/teatree``), which is not a git repository at all — so reading the
invoking branch from ``"."`` recorded nothing, and the ship fell through to
``worktrees.first()``: another repo's row entirely. ``deploy/t3`` translates the
host cwd into container coordinates and exports it; ``invocation_cwd()`` is the
only reader that sees it.
"""

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree import visual_qa
from teatree.core.invocation_cwd import INVOCATION_CWD_ENV
from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Session, Ticket, Worktree

from ._shared import _MOCK_OVERLAY


def _git_repo_on_branch(root: Path, branch: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        subprocess.run(args, cwd=root, check=True, capture_output=True)

    run("git", "init", "--quiet")
    run("git", "config", "user.email", "t@example.test")
    run("git", "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n")
    run("git", "add", "-A")
    run("git", "commit", "--quiet", "-m", "seed")
    run("git", "checkout", "--quiet", "-b", branch)
    return root


def _attest_shipping_phases(ticket: Ticket) -> None:
    """Satisfy the #694 phase gate so the run reaches the diff-rendering gates."""
    session = Session.objects.create(ticket=ticket, overlay="test")
    for phase in ("testing", "reviewing", "retro"):
        session.visit_phase(phase)


class TestInvokingBranchComesFromInvocationCwd(TestCase):
    """The recorded ship branch is the DECLARED cwd's branch, not the process cwd's."""

    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch

    def test_records_the_declared_cwd_branch(self) -> None:
        checkout = _git_repo_on_branch(self._tmp_path / "frontend", "8680-testkonzept")
        self._monkeypatch.setenv(INVOCATION_CWD_ENV, str(checkout))

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        Session.objects.create(ticket=ticket, overlay="test")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="frontend",
            branch="8680-testkonzept",
            extra={"worktree_path": str(checkout)},
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.tasks.execute_ship", MagicMock()),
        ):
            pr_command.Command().create(str(ticket.id))

        ticket.refresh_from_db()
        assert (ticket.extra or {}).get("ship_invoking_branch") == "8680-testkonzept"


class TestAdoptWorktreeAttachesTheInvokingOne(TestCase):
    """``--adopt-worktree`` attaches the invoking worktree even when rows exist."""

    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch

    def test_does_not_short_circuit_on_an_unrelated_repos_row(self) -> None:
        checkout = _git_repo_on_branch(self._tmp_path / "frontend", "8680-testkonzept")
        self._monkeypatch.setenv(INVOCATION_CWD_ENV, str(checkout))

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feat/stale-earlier-workstream",
            extra={"worktree_path": str(self._tmp_path / "backend")},
        )

        adopt = MagicMock(
            side_effect=lambda _ticket, *, cwd: Worktree.objects.create(
                ticket=_ticket,
                overlay="test",
                repo_path="frontend",
                branch="8680-testkonzept",
                extra={"worktree_path": cwd},
            )
        )
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.management.commands._pr_worktree.adopt_worktree_for_ticket", adopt),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.tasks.execute_ship", MagicMock()),
        ):
            pr_command.Command().create(str(ticket.id), adopt_worktree=True)

        adopt.assert_called_once()
        assert adopt.call_args.kwargs["cwd"] == str(checkout)

    def test_an_already_recorded_invoking_worktree_resolves_without_adopting(self) -> None:
        checkout = _git_repo_on_branch(self._tmp_path / "frontend", "8680-testkonzept")
        self._monkeypatch.setenv(INVOCATION_CWD_ENV, str(checkout))

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="backend",
            branch="feat/stale-earlier-workstream",
            extra={"worktree_path": str(self._tmp_path / "backend")},
        )
        already = Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="frontend",
            branch="8680-testkonzept",
            extra={"worktree_path": str(checkout)},
        )

        adopt = MagicMock()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.management.commands._pr_worktree.adopt_worktree_for_ticket", adopt),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch("teatree.core.tasks.execute_ship", MagicMock()),
        ):
            pr_command.Command().create(str(ticket.id), adopt_worktree=True)

        adopt.assert_not_called()
        assert ticket.worktrees.filter(pk=already.pk).exists()


class TestSharedBranchNameShipsTheInvokingRepo(TestCase):
    """The canonical layout gives every repo of a ticket the SAME branch name.

    ``workspace ticket`` provisions ``<workspace>/<branch>/<repo-leaf>`` and mints
    ``Worktree.branch`` as ``<N>-ticket`` per repo, so branch-name matching alone
    resolves the earliest row — shipping a repo the operator never named, with no
    refusal. The invoking cwd is what tells the rows apart.
    """

    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch

    def test_dry_run_ships_the_repo_the_operator_stands_in(self) -> None:
        backend = _git_repo_on_branch(self._tmp_path / "9001-ticket" / "backend", "9001-ticket")
        frontend = _git_repo_on_branch(self._tmp_path / "9001-ticket" / "frontend", "9001-ticket")
        self._monkeypatch.setenv(INVOCATION_CWD_ENV, str(frontend))

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.test/-/issues/9001",
            state=Ticket.State.REVIEWED,
        )
        _attest_shipping_phases(ticket)
        for checkout in (backend, frontend):
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path=str(checkout),
                branch="9001-ticket",
                extra={"worktree_path": str(checkout)},
            )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", pr_command.Command().create(str(ticket.id), dry_run=True))

        assert result.get("repo") == str(frontend), f"unexpected result: {result}"


class TestGatesSurviveTheBranchReconcile(TestCase):
    """A rename reconciled mid-``create`` must not strand the gates on a dead key.

    ``resolve_ship_target`` resolves the row, then ``resolve_and_reconcile_branch``
    rewrites ``Worktree.branch`` to the worktree's real git branch. The gates then
    re-resolved from ``extra['ship_invoking_branch']``, which the reconcile left
    naming the old name — matching no row, so a multi-repo ticket raised
    ``ShipWorktreeAmbiguousError`` straight out of the ``manage.py`` subprocess.
    """

    @pytest.fixture(autouse=True)
    def _inject(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp_path = tmp_path
        self._monkeypatch = monkeypatch

    def test_pr_create_returns_a_result_instead_of_crashing(self) -> None:
        checkout = _git_repo_on_branch(self._tmp_path / "9001-ticket" / "backend", "9001-feat-be")
        outside = self._tmp_path / "outside"
        outside.mkdir()
        self._monkeypatch.setenv(INVOCATION_CWD_ENV, str(outside))

        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.test/-/issues/9001",
            state=Ticket.State.REVIEWED,
            extra={"ship_invoking_branch": "9001-ticket"},
        )
        _attest_shipping_phases(ticket)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(checkout),
            branch="9001-ticket",
            extra={"worktree_path": str(checkout)},
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=str(self._tmp_path / "9001-ticket" / "frontend"),
            branch="9001-feat-fe",
            extra={"worktree_path": str(self._tmp_path / "9001-ticket" / "frontend")},
        )

        scanned: dict[str, str] = {}

        def fake_changed_files(*, repo: str) -> list[str]:
            scanned["repo"] = repo
            return []

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
            patch.object(visual_qa, "changed_files", side_effect=fake_changed_files),
        ):
            result = cast("dict[str, object]", pr_command.Command().create(str(ticket.id), dry_run=True))

        assert result.get("repo") == str(checkout), f"unexpected result: {result}"
        assert result.get("branch") == "9001-feat-be", f"unexpected result: {result}"
        # #776 N1 end to end: the gate scanned the repo the ship resolved.
        assert scanned["repo"] == str(checkout)


class TestMultiRepoTicketRefusalReachesTheOperator(TestCase):
    """The ambiguity surfaces as a named refusal, never another repo's stale branch."""

    def test_pr_create_returns_the_ambiguity_message(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.NOT_STARTED)
        for repo, branch in (("backend", "feat/stale-earlier-workstream"), ("frontend", "8680-testkonzept")):
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path=repo,
                branch=branch,
                extra={"worktree_path": f"/tmp/{repo}"},
            )

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", pr_command.Command().create(str(ticket.id)))

        error = str(result["error"])
        assert "spans 2 repos" in error
        assert "backend" in error
        assert "frontend" in error
