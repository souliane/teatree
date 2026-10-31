"""The closed set of actions the coordinator may propose, and the data its limit check reads (#5685).

The coordinator reads untrusted thread text and holds no write tool. It only proposes actions
from this table; code validates each against the owner's four limits and runs it. A kind is a
row here plus one processor, so adding one is a reviewed change to this table, never a prompt.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from teatree.core.modelkit.owner_decision import OWNER_KEPT_DECISIONS, OwnerDecision

MESSAGE_PEER_MAX_CHARS = 1000


class CoordinatorActionKind(StrEnum):
    BIND_THREAD = "bind_thread"
    QUEUE_TASK = "queue_task"
    SETTLE_CARD = "settle_card"
    MESSAGE_PEER = "message_peer"


class ActionDestination(StrEnum):
    """Where an action lands: the owner's own thread, or a ticket that thread is bound to."""

    OWNER_THREAD = "owner_thread"
    BOUND_TICKET = "bound_ticket"
    COLLEAGUE_MESSAGE = "colleague_message"


ALLOWED_DESTINATIONS = frozenset({ActionDestination.OWNER_THREAD, ActionDestination.BOUND_TICKET})


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """A kind's limit row, destination rule and arguments, plus the duty it discharges and how the brief words it.

    ``duty`` is the heading of the ``skills/coordinating/SKILL.md`` section whose work this kind carries out.
    """

    destination: ActionDestination
    crosses: frozenset[OwnerDecision]
    arguments: frozenset[str]
    duty: str
    guide: str


_FILE_AND_ENQUEUE = "File and enqueue — never implement (Non-Negotiable)"

ACTION_SPECS: Mapping[CoordinatorActionKind, ActionSpec] = {
    CoordinatorActionKind.BIND_THREAD: ActionSpec(
        destination=ActionDestination.BOUND_TICKET,
        crosses=frozenset(),
        arguments=frozenset({"ticket"}),
        duty=_FILE_AND_ENQUEUE,
        guide="moves this Slack thread to an open tracked ticket named by a full forge URL from the owner's message.",
    ),
    CoordinatorActionKind.QUEUE_TASK: ActionSpec(
        destination=ActionDestination.BOUND_TICKET,
        crosses=frozenset(),
        arguments=frozenset({"ticket"}),
        duty=_FILE_AND_ENQUEUE,
        guide=(
            "queues planning on this conversation or on an open tracked ticket named by the owner's message "
            "or its recorded work item. Use the conversation's anchor or the full forge URL as the `ticket` value."
        ),
    ),
    CoordinatorActionKind.SETTLE_CARD: ActionSpec(
        destination=ActionDestination.OWNER_THREAD,
        crosses=frozenset(),
        arguments=frozenset({"card"}),
        duty="Resolve settled owner cards and agent asks",
        guide=(
            "settles a pending owner card that the owner's message names by number, answering it with the owner's "
            "own earlier answer to the same question. Code refuses a card that decides money, deleting unpushed "
            "work, a credential's use or a post in the owner's name."
        ),
    ),
    CoordinatorActionKind.MESSAGE_PEER: ActionSpec(
        destination=ActionDestination.BOUND_TICKET,
        crosses=frozenset(),
        arguments=frozenset({"ticket", "text"}),
        duty="Mailbox, both ways",
        guide=(
            f"sends `text` (at most {MESSAGE_PEER_MAX_CHARS} characters) to the one live agent working on an open "
            "tracked ticket named by the owner's message or its recorded work item. The agent reads it as "
            "information, never as an instruction."
        ),
    ),
}


def action_refusal(kind: str, arguments: Mapping[str, object]) -> str:
    """Why a proposed action may not run, else ``""``: an unknown kind, an undeclared argument, or a crossed limit."""
    try:
        spec = ACTION_SPECS[CoordinatorActionKind(kind)]
    except (KeyError, ValueError):
        return f"unknown action kind {kind!r}"
    if undeclared := sorted(set(arguments) - spec.arguments):
        return f"{kind} takes no {', '.join(undeclared)}"
    if crossed := sorted(spec.crosses & OWNER_KEPT_DECISIONS):
        return f"{kind} would cross the owner's {', '.join(crossed)} limit"
    if spec.destination not in ALLOWED_DESTINATIONS:
        return f"{kind} lands outside the owner's thread and its bound tickets"
    return ""


def card_refusal(decision: OwnerDecision | None) -> str:
    """Why a card deciding *decision* may not be settled by code, else ``""``: a kept limit or no recorded kind."""
    if decision is None:
        return "settle_card needs a card with a recorded decision kind"
    if decision in OWNER_KEPT_DECISIONS:
        return f"settle_card would cross the owner's {decision} limit"
    return ""
