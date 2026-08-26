"""``resolve_ship_worktree`` — never silently pick another repo's row.

A ticket spanning N repos has N ``Worktree`` rows. With no invoking branch
recorded, the resolver used to fall through to ``worktrees.first()`` — the
EARLIEST row, which on a multi-repo ticket is routinely a different repo than
the one the operator is standing in. The ship then reported "0 commits ahead"
against a branch the operator never named, sending them to the wrong repo.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.runners.ship import ShipWorktreeAmbiguousError, resolve_ship_worktree


class TestResolveShipWorktree(TestCase):
    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="test", issue_url="https://example.test/-/issues/8680")

    def _row(self, ticket: Ticket, repo: str, branch: str) -> Worktree:
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=repo,
            branch=branch,
            extra={"worktree_path": f"/tmp/{repo}"},
        )

    def test_multi_repo_ticket_with_no_invoking_branch_refuses_and_names_the_repos(self) -> None:
        ticket = self._ticket()
        self._row(ticket, "backend", "feat/stale-earlier-workstream")
        self._row(ticket, "frontend", "8680-feat-thing")

        with pytest.raises(ShipWorktreeAmbiguousError) as excinfo:
            resolve_ship_worktree(ticket, {})

        message = str(excinfo.value)
        assert "backend" in message
        assert "frontend" in message
        # The refusal must not silently resolve to the earliest row's branch.
        assert "feat/stale-earlier-workstream" not in message.split("candidates", maxsplit=1)[0]

    def test_invoking_branch_selects_its_own_row_on_a_multi_repo_ticket(self) -> None:
        ticket = self._ticket()
        self._row(ticket, "backend", "feat/stale-earlier-workstream")
        wanted = self._row(ticket, "frontend", "8680-feat-thing")

        assert resolve_ship_worktree(ticket, {"ship_invoking_branch": "8680-feat-thing"}) == wanted

    def test_single_repo_ticket_still_resolves_without_an_invoking_branch(self) -> None:
        ticket = self._ticket()
        only = self._row(ticket, "backend", "8680-ticket")

        assert resolve_ship_worktree(ticket, {}) == only

    def test_several_rows_in_one_repo_are_not_ambiguous(self) -> None:
        ticket = self._ticket()
        first = self._row(ticket, "backend", "8680-ticket")
        self._row(ticket, "backend", "8680-followup")

        assert resolve_ship_worktree(ticket, {}) == first

    def test_ticket_with_no_rows_returns_none(self) -> None:
        assert resolve_ship_worktree(self._ticket(), {}) is None


class TestSharedBranchNameIsNotAnIdentity(TestCase):
    """Teatree's own layout mints ONE branch name across every repo of a ticket.

    ``workspace ticket`` provisions ``<workspace>/<branch>/<repo-leaf>`` and mints
    ``Worktree.branch`` as ``<N>-ticket`` for each repo, so a multi-repo ticket
    routinely carries the SAME branch name on two rows. Matching on the name alone
    then resolves the earliest row — the very silent wrong-repo ship the refusal
    exists to prevent — so the invoking PATH is the identity and the name is only a
    fallback.
    """

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(overlay="test", issue_url="https://example.test/-/issues/9001")

    def _row(self, ticket: Ticket, repo: str, branch: str, path: str) -> Worktree:
        return Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=repo,
            branch=branch,
            extra={"worktree_path": path},
        )

    def test_invoking_path_selects_its_own_row_when_both_repos_share_the_branch(self) -> None:
        ticket = self._ticket()
        self._row(ticket, "backend", "9001-ticket", "/tmp/9001-ticket/backend")
        wanted = self._row(ticket, "frontend", "9001-ticket", "/tmp/9001-ticket/frontend")

        resolved = resolve_ship_worktree(
            ticket,
            {"ship_invoking_branch": "9001-ticket", "ship_invoking_path": "/tmp/9001-ticket/frontend"},
        )

        assert resolved == wanted

    def test_shared_branch_name_with_no_invoking_path_refuses(self) -> None:
        ticket = self._ticket()
        self._row(ticket, "backend", "9001-ticket", "/tmp/9001-ticket/backend")
        self._row(ticket, "frontend", "9001-ticket", "/tmp/9001-ticket/frontend")

        with pytest.raises(ShipWorktreeAmbiguousError) as excinfo:
            resolve_ship_worktree(ticket, {"ship_invoking_branch": "9001-ticket"})

        message = str(excinfo.value)
        assert "backend" in message
        assert "frontend" in message

    def test_invoking_path_wins_over_a_branch_naming_another_repos_row(self) -> None:
        ticket = self._ticket()
        self._row(ticket, "backend", "9001-feat-be", "/tmp/9001-ticket/backend")
        wanted = self._row(ticket, "frontend", "9001-feat-fe", "/tmp/9001-ticket/frontend")

        resolved = resolve_ship_worktree(
            ticket,
            {"ship_invoking_branch": "9001-feat-be", "ship_invoking_path": "/tmp/9001-ticket/frontend"},
        )

        assert resolved == wanted

    def test_a_stale_invoking_branch_still_resolves_through_the_recorded_path(self) -> None:
        """The reconcile renames a BRANCH; the worktree directory never moves."""
        ticket = self._ticket()
        self._row(ticket, "backend", "9001-feat-be", "/tmp/9001-ticket/backend")
        self._row(ticket, "frontend", "9001-feat-fe", "/tmp/9001-ticket/frontend")

        resolved = resolve_ship_worktree(
            ticket,
            {"ship_invoking_branch": "9001-ticket", "ship_invoking_path": "/tmp/9001-ticket/backend"},
        )

        assert resolved is not None
        assert resolved.repo_path == "backend"
