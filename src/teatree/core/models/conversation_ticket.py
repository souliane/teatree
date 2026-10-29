"""A Slack conversation's bookkeeping ticket: how long it lives and which task phases it may host (#5685).

The ticket a Slack lane mints is anchored on the synthetic loop umbrella, not on a forge
issue, so nothing findable ever tracks it and no plan or PR can be built on it. It hosts the
talk (answering, planning); the build phases belong to the forge ticket the request is
promoted into, which takes over the thread's binding.
"""

from datetime import timedelta
from typing import TYPE_CHECKING

from teatree.core.modelkit.phases import IMPLEMENTING_PHASES, normalize_phase
from teatree.core.models.errors import TaskMintRefusedError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

#: How long a thread with no live task stays a conversation; a later reply inside it finds its ticket.
CONVERSATION_QUIET_LIMIT = timedelta(days=7)

CONVERSATION_REFUSED_PREFIX = "conversation_ticket: "

_BUILD_PHASES = IMPLEMENTING_PHASES | {"shipping"}


def conversation_refusal(ticket: "Ticket", *, phase: str) -> str | None:
    """The refusal for a build *phase* on a conversation ticket, else ``None``."""
    canonical = normalize_phase(phase)
    if canonical not in _BUILD_PHASES or not ticket.is_conversation():
        return None
    return (
        f"{CONVERSATION_REFUSED_PREFIX}refusing a {canonical} task on ticket {ticket.pk} — it is a Slack "
        f"conversation, not a tracked work item, so nothing built on it could be found or shipped. File the "
        f"request as a work item first; the promotion moves the thread to that ticket, which takes the task."
    )


def refuse_conversation_mint(ticket: "Ticket", *, phase: str) -> None:
    refusal = conversation_refusal(ticket, phase=phase)
    if refusal is not None:
        raise TaskMintRefusedError(refusal)
