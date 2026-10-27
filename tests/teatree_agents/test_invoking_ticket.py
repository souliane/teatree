import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from teatree.agents.invoking_ticket import resolve_invoking_ticket
from teatree.core.models import Ticket, Worktree


class TestInvokingTicket(TestCase):
    def test_room_precedes_environment_and_registered_worktree(self) -> None:
        room = Ticket.objects.create(overlay="teatree")
        env = Ticket.objects.create(overlay="teatree")
        worktree = Ticket.objects.create(overlay="teatree")
        path = Path("/tmp/registered-ticket-worktree")
        Worktree.objects.create(
            ticket=worktree, repo_path="project", branch="ticket", extra={"worktree_path": str(path)}
        )

        async def self_identity() -> dict[str, str]:
            await asyncio.sleep(0)
            return {"room": str(room.pk)}

        with (
            patch(
                "teatree.agents.invoking_ticket.MailboxClient.from_env",
                return_value=SimpleNamespace(self_identity=self_identity),
            ),
            patch.dict("os.environ", {"T3_TICKET_ID": str(env.pk)}),
        ):
            assert resolve_invoking_ticket(cwd=path).pk == room.pk

    def test_environment_then_registered_worktree(self) -> None:
        env = Ticket.objects.create(overlay="teatree")
        worktree = Ticket.objects.create(overlay="teatree")
        path = Path("/tmp/registered-ticket-worktree")
        Worktree.objects.create(
            ticket=worktree, repo_path="project", branch="ticket", extra={"worktree_path": str(path)}
        )
        with (
            patch("teatree.agents.invoking_ticket.MailboxClient.from_env", return_value=None),
            patch.dict("os.environ", {"T3_TICKET_ID": str(env.pk)}),
        ):
            assert resolve_invoking_ticket(cwd=path).pk == env.pk
        with (
            patch("teatree.agents.invoking_ticket.MailboxClient.from_env", return_value=None),
            patch.dict("os.environ", {"T3_TICKET_ID": ""}),
        ):
            assert resolve_invoking_ticket(cwd=path / "src").pk == worktree.pk
