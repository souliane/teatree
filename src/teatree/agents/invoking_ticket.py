import asyncio
import os
from pathlib import Path

from teatree.agents.live_delivery import TICKET_ID_ENV
from teatree.agents.live_mailbox_client import MailboxClient
from teatree.core.models import Ticket, Worktree


def _ticket_by_pk(value: str) -> Ticket | None:
    return Ticket.objects.filter(pk=int(value)).first() if value.isdecimal() else None


def resolve_invoking_ticket(*, cwd: Path | None = None) -> Ticket | None:
    client = MailboxClient.from_env()
    if client is not None:
        try:
            room = asyncio.run(client.self_identity()).get("room", "")
        except (OSError, RuntimeError, ValueError):
            room = ""
        if ticket := _ticket_by_pk(room):
            return ticket
    if ticket := _ticket_by_pk(os.environ.get(TICKET_ID_ENV, "")):
        return ticket
    path = (cwd or Path.cwd()).resolve()
    for worktree in Worktree.objects.exclude(extra__worktree_path__isnull=True).select_related("ticket"):
        if worktree.worktree_path and path.is_relative_to(Path(worktree.worktree_path).resolve()):
            return worktree.ticket
    return None
