"""``pr create`` enforces the per-(repo, ticket) open-PR budget (north-star PR-2).

The budget gate is wired into ``_run_ship_gates`` right after the shipping gate:
the ``run_pr_budget_gate`` adapter resolves the ship repo's slug and delegates to
the core ``check_pr_budget`` policy. Anti-vacuous at the seam: the SAME wired
adapter blocks when the overlay capped the ticket at its open-PR budget and is a
no-op at the neutral default — the setting is what flips the outcome.
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates import pr_budget_gate
from teatree.core.management.commands import _ship_gates as ship_gates_mod
from teatree.core.management.commands._ship_gates import run_pr_budget_gate
from teatree.core.models import PullRequest, Ticket, Worktree

from ._shared import _MOCK_OVERLAY, _shippable_ticket

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)

_SLUG = "souliane/teatree"


def _capped(limit: int) -> UserSettings:
    return UserSettings(max_open_prs_per_repo_per_ticket=limit)


def _open_pr(ticket: Ticket, *, iid: str, repo: str = _SLUG) -> PullRequest:
    return PullRequest.objects.create(
        ticket=ticket,
        url=f"https://github.com/{repo}/pull/{iid}",
        repo=repo,
        iid=iid,
        overlay=ticket.overlay,
    )


class TestRunPrBudgetGate(TestCase):
    """The ship-chain adapter: resolve slug -> delegate -> typed failure or None."""

    def _worktree(self, ticket: Ticket) -> Worktree:
        return Worktree.objects.create(
            ticket=ticket,
            overlay=ticket.overlay,
            repo_path="/tmp/wt",
            branch="feat",
            extra={"worktree_path": "/tmp/wt"},
        )

    def test_returns_failure_naming_the_url_when_at_cap(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        pr = _open_pr(ticket, iid="1")
        with (
            patch.object(ship_gates_mod.git, "remote_slug", return_value=_SLUG),
            patch.object(pr_budget_gate, "get_effective_settings", return_value=_capped(1)),
        ):
            result = run_pr_budget_gate(ticket, worktree)
        assert result is not None
        assert result["allowed"] is False
        assert pr.url in result["error"]

    def test_inert_at_neutral_default_even_with_open_prs(self) -> None:
        # Wired but inert: default 0 -> no failure though an open PR exists.
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        _open_pr(ticket, iid="1")
        with (
            patch.object(ship_gates_mod.git, "remote_slug", return_value=_SLUG),
            patch.object(pr_budget_gate, "get_effective_settings", return_value=_capped(0)),
        ):
            assert run_pr_budget_gate(ticket, worktree) is None

    def test_no_op_when_slug_unresolvable(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        _open_pr(ticket, iid="1")
        with (
            patch.object(ship_gates_mod.git, "remote_slug", return_value=""),
            patch.object(pr_budget_gate, "get_effective_settings", return_value=_capped(1)),
        ):
            assert run_pr_budget_gate(ticket, worktree) is None

    def test_allows_when_the_open_pr_is_in_a_different_repo(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        worktree = self._worktree(ticket)
        _open_pr(ticket, iid="1", repo="souliane/other")
        with (
            patch.object(ship_gates_mod.git, "remote_slug", return_value=_SLUG),
            patch.object(pr_budget_gate, "get_effective_settings", return_value=_capped(1)),
        ):
            assert run_pr_budget_gate(ticket, worktree) is None


class TestPrCreatePrBudgetWiring(TestCase):
    """End-to-end proof the gate is live in ``_run_ship_gates`` under ``pr create``."""

    def test_pr_create_blocks_a_second_open_pr_for_the_same_repo_ticket(self) -> None:
        ticket = _shippable_ticket()
        _open_pr(ticket, iid="1")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(ship_gates_mod.git, "remote_slug", return_value=_SLUG),
            patch.object(pr_budget_gate, "get_effective_settings", return_value=_capped(1)),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))
        ticket.refresh_from_db()
        assert result.get("allowed") is False
        assert "max_open_prs_per_repo_per_ticket" in str(result.get("error"))
        assert f"{_SLUG}/pull/1" in str(result.get("error"))
        assert ticket.state != Ticket.State.SHIPPED
