"""Hand a coordinator's ``message_peer`` to the one live agent working on a ticket, over the mailbox (#5685)."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from teatree.agents import live_mailbox
from teatree.agents.coordinator_escalation import is_answering_peer
from teatree.agents.live_control import PeerIdentity
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.answering.coordinator_actions import PeerReceipt
from teatree.core.models import Ticket

logger = logging.getLogger(__name__)

_ACCEPTANCE_SECONDS = 2
_NO_TICKET_ROOM = "0"


def _candidates(peers: list[PeerIdentity], ticket_pk: int) -> list[PeerIdentity]:
    return [
        peer
        for peer in peers
        if peer.get("ticket") == str(ticket_pk) and peer.get("can_receive", True) and not is_answering_peer(peer)
    ]


async def _deliver(client: MailboxClient, ticket_pk: int, text: str, key: str) -> PeerReceipt:
    candidates = _candidates(await client.peers(), ticket_pk)
    if not candidates:
        return PeerReceipt(accepted=False, detail="no live agent is working on that ticket")
    if len(candidates) > 1:
        return PeerReceipt(accepted=False, detail="more than one live agent is working on that ticket")
    receipt = await client.send(candidates[0]["address"], text, key, timeout_seconds=_ACCEPTANCE_SECONDS)
    if receipt["outcome"].startswith("accepted_"):
        return PeerReceipt(accepted=True, detail="")
    if receipt["queued_in_inbox"]:
        return PeerReceipt(accepted=False, detail="the agent has not read it yet; it stays queued")
    return PeerReceipt(accepted=False, detail=f"the agent did not take it ({receipt['code'] or receipt['outcome']})")


def send_to_ticket_peer(ticket: Ticket, text: str, key: str) -> PeerReceipt:
    try:
        broker = live_mailbox.shared_broker()
        sender = broker.register(room=_NO_TICKET_ROOM, harness="coordinator", label="coordinator message")
        try:
            # A fresh thread: the caller may already sit inside a running event loop, where asyncio.run refuses.
            client = MailboxClient(sender.socket_path, sender.token)
            with ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(lambda: asyncio.run(_deliver(client, ticket.pk, text, key))).result()
        finally:
            broker.unregister(sender.address)
    except (MailboxError, OSError, RuntimeError, ValueError):
        logger.warning("Coordinator message for ticket %s was not handed to the live mailbox", ticket.pk, exc_info=True)
        return PeerReceipt(accepted=False, detail="the live mailbox could not be reached")
