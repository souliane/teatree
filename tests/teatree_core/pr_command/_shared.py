"""Shared doubles for the teatree.core ``pr`` command test package.

Lifted verbatim from the former monolithic
``tests/teatree_core/test_pr_command.py`` (souliane/teatree#443). No
behavior change: the same mock-overlay registry, shippable-ticket
builder and immediate-tasks override settings, relocated so each
focused test module can import them.
"""

from teatree.core.models import Session, Ticket, Worktree
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


def _shippable_ticket() -> Ticket:
    """Build a ticket pre-advanced to REVIEWED with the shipping gate satisfied."""
    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
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


_SHIP_BACKEND = {"TASKS": {"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}}}
