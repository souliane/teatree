"""The closed set of actions the coordinator may propose, and the data its limit check reads (#5685).

The coordinator reads untrusted thread text and holds no write tool. It only proposes actions
from this table; code validates each against the owner's four limits and runs it. A kind is a
row here plus one processor, so adding one is a reviewed change to this table, never a prompt.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from teatree.core.modelkit.owner_decision import OWNER_KEPT_DECISIONS, OwnerDecision


class CoordinatorActionKind(StrEnum):
    BIND_THREAD = "bind_thread"
    QUEUE_TASK = "queue_task"


class ActionDestination(StrEnum):
    """Where an action lands: the owner's own thread, or a ticket that thread is bound to."""

    OWNER_THREAD = "owner_thread"
    BOUND_TICKET = "bound_ticket"
    COLLEAGUE_MESSAGE = "colleague_message"


ALLOWED_DESTINATIONS = frozenset({ActionDestination.OWNER_THREAD, ActionDestination.BOUND_TICKET})


@dataclass(frozen=True, slots=True)
class ActionSpec:
    destination: ActionDestination
    crosses: frozenset[OwnerDecision]
    arguments: frozenset[str]


ACTION_SPECS: Mapping[CoordinatorActionKind, ActionSpec] = {
    CoordinatorActionKind.BIND_THREAD: ActionSpec(
        destination=ActionDestination.BOUND_TICKET,
        crosses=frozenset(),
        arguments=frozenset({"ticket"}),
    ),
    CoordinatorActionKind.QUEUE_TASK: ActionSpec(
        destination=ActionDestination.BOUND_TICKET,
        crosses=frozenset(),
        arguments=frozenset({"ticket"}),
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
