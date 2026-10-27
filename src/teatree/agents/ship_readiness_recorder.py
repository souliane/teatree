"""Ship what a read-only executor run judged ready, through the worker's own ``pr create`` gate chain.

The executor can push nothing, so it returns a ``ship_readiness`` verdict and stops. A verdict
that is not ``ready`` fails the task here, so the completion path that fires ``Ticket.ship`` never
runs for it. A ready verdict is acted on by the worker alone: ``pr create`` runs every ship gate
and queues ``execute_ship``, the existing worker job that pushes the branch and opens the pull request.
"""

import io
from collections.abc import Callable, Mapping
from typing import Final

from teatree.agents.result_schema import AgentResultBlob
from teatree.core.machine_output import call_command_streamed
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import Task

NOT_READY_PREFIX: Final = "ship_not_ready: "

type TicketShipper = Callable[[str], object]


def ship_through_command(ticket_id: str) -> object:
    """``pr create`` in the worker: the ship gates, the FSM transition and the queued ``execute_ship``."""
    return call_command_streamed("pr", "create", ticket_id, stream=io.StringIO())


def record_returned_ship_readiness(
    task: Task, result: AgentResultBlob, *, phase: str, shipper: TicketShipper = ship_through_command
) -> str:
    """Queue *task*'s ship when the executor judged it ready; return a refusal otherwise, or ``""`` once queued.

    A no-op on every other phase and on a result that returned no verdict (the evidence gate owns that).
    """
    if normalize_phase(phase or task.phase) != "shipping" or "ship_readiness" not in result:
        return ""
    verdict = result["ship_readiness"]
    if not isinstance(verdict, Mapping):
        return f"{NOT_READY_PREFIX}expected an object, got {type(verdict).__name__}"
    if verdict.get("ready") is not True:
        return f"{NOT_READY_PREFIX}the executor judged the branch not ready: {_reasons(verdict)}"
    outcome = shipper(str(task.ticket_id))
    if isinstance(outcome, Mapping) and outcome.get("queued") is True:
        return ""
    detail = (
        (outcome.get("error") or outcome.get("reason") or dict(outcome)) if isinstance(outcome, Mapping) else outcome
    )
    return f"{NOT_READY_PREFIX}the ship gates refused it: {detail}"


def _reasons(verdict: Mapping[str, object]) -> str:
    reasons = verdict.get("reasons")
    return "; ".join(str(reason) for reason in reasons) if isinstance(reasons, list) and reasons else "no reason given"
