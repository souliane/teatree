"""Shared doubles for the teatree.core ``pr`` command test package.

Lifted verbatim from the former monolithic
``tests/teatree_core/test_pr_command.py`` (souliane/teatree#443). No
behavior change: the same mock-overlay registry, shippable-ticket
builder and immediate-tasks override settings, relocated so each
focused test module can import them.
"""

from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from teatree.core.management.commands import pr as pr_command
from teatree.core.models import Session, Ticket, Worktree
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}

# A commit subject + body that satisfy the deterministic MR title/description
# gate (#1540), so a test exercising an unrelated invariant (FSM reconcile, no
# raw transition) is not tripped by the now-active format check.
_CONFORMING_COMMIT = ("feat(ship): recover and ship", "## What\nx\n\n## Why\ny")
_PR_URL = "https://github.com/acme/backend/pull/7"


def _code_host() -> MagicMock:
    host = MagicMock()
    host.current_user.return_value = "tester"
    host.create_pr.return_value = {"web_url": _PR_URL}
    return host


@contextmanager
def _ship_externals(host: MagicMock) -> Generator[MagicMock]:
    """Stub only what a real ship reaches outside the process: the overlay registry, the forge, git, the push."""
    with (
        patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
        patch("teatree.core.runners.ship.code_host_for_repo_from_overlay", return_value=host),
        patch("teatree.core.runners.ship.branch_is_landed", return_value=False),
        patch("teatree.core.runners.ship.sha_conflicts_with_target", return_value=None),
        patch("teatree.core.runners.ship.git.last_commit_message", return_value=_CONFORMING_COMMIT),
        patch("teatree.core.runners.ship.push_branch") as push,
    ):
        yield push


@contextmanager
def _gates_pass() -> Generator[None]:
    with (
        patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
        patch.object(pr_command, "validate_pr_metadata", return_value=None),
    ):
        yield


def _shippable_ticket() -> Ticket:
    """Build a ticket pre-advanced to SELF_REVIEWED with the shipping gate satisfied."""
    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.SELF_REVIEWED)
    session = Session.objects.create(ticket=ticket, overlay="test")
    session.visit_phase("testing")
    session.visit_phase("reviewing")
    session.visit_phase("retro")
    Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path="/tmp/backend",
        branch="feature-branch",
        extra={"worktree_path": "/tmp/backend"},
    )
    return ticket


_SHIP_BACKEND = {"TASKS": {"default": {"BACKEND": "django.tasks.backends.immediate.ImmediateBackend"}}}
