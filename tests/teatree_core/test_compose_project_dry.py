"""Every compose-project name derives through one helper (Follow-ups from #1998).

The docker-compose project name ``<repo_path>-wt<ticket_number>`` was
re-derived inline in several core modules (the stack gate, the env cache,
the reconciler) instead of resolving through the single
``compose_project`` helper. A duplicated derivation drifts silently — a
later change to the naming scheme in one place breaks the others without
any error. This pins that every module resolves the same canonical key.
"""

from dataclasses import dataclass
from typing import cast
from unittest.mock import patch

from django.test import TestCase

from teatree.core import local_stack_gate as gate_mod
from teatree.core import reconcile as reconcile_mod
from teatree.core import worktree_env as worktree_env_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree_env import compose_project


@dataclass
class _TicketlessWorktree:
    """Duck-typed worktree with no ticket — mirrors the external-overlay probe path."""

    repo_path: str
    ticket: object | None = None


def _make_worktree(*, ticket_number: str, repo_path: str = "backend") -> Worktree:
    ticket = Ticket.objects.create(
        issue_url=f"https://example.com/issues/{ticket_number}",
        overlay="test",
    )
    return Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path=repo_path,
        branch=f"{ticket_number}-feat",
        state=Worktree.State.PROVISIONED,
        extra={"worktree_path": f"/ws/{ticket_number}-feat/{repo_path}"},
    )


class TestComposeProjectSingleSource(TestCase):
    def test_helper_qualifies_repo_and_ticket(self) -> None:
        wt = _make_worktree(ticket_number="9001", repo_path="frontend")
        assert compose_project(wt) == "frontend-wt9001"

    def test_helper_falls_back_to_repo_path_without_ticket(self) -> None:
        wt = _TicketlessWorktree(repo_path="backend")
        assert compose_project(cast("Worktree", wt)) == "backend"

    def test_stack_gate_uses_the_helper(self) -> None:
        wt = _make_worktree(ticket_number="9010")
        with patch.object(gate_mod, "_running_container_count", return_value=1) as probe:
            gate_mod._reconcile_phantom_blocker(wt)
        probe.assert_called_once_with(compose_project(wt))

    def test_env_cache_compose_project_name_matches_helper(self) -> None:
        wt = _make_worktree(ticket_number="9020")
        pairs = dict(worktree_env_mod._core_env_pairs(wt))
        assert pairs["COMPOSE_PROJECT_NAME"] == compose_project(wt)

    def test_reconciler_orphan_lookup_uses_the_helper(self) -> None:
        wt = _make_worktree(ticket_number="9030")
        wt.state = Worktree.State.CREATED
        wt.save(update_fields=["state"])
        from teatree.core.reconcile import Drift  # noqa: PLC0415

        drift = Drift(ticket_pk=wt.ticket.pk)
        with (
            patch.object(reconcile_mod, "render_env_cache", return_value=None),
            patch.object(reconcile_mod, "_find_docker_containers", return_value=[]) as probe,
        ):
            reconcile_mod._reconcile_worktree_row(drift, wt)
        probe.assert_called_once_with(compose_project(wt))
