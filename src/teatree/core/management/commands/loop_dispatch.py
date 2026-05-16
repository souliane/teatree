"""``manage.py loop_dispatch`` — read & claim pending agent dispatches.

The DB is the dispatch queue: when ``run_tick`` produces a
``kind="agent"`` action, ``teatree.loop.persistence`` creates a Ticket
+ Task row. The ``/loop`` slot's session reads pending Tasks via
``pending-spawn``, calls its ``Agent`` tool once per entry, then claims
each via ``spawn-claim`` so the next tick doesn't see them as pending.
"""

import json
from typing import Annotated, Any

import typer
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django_typer.management import TyperCommand, command

from teatree.core.models import Task, Ticket

# Maps ticket role + task phase → the sub-agent the slot should Agent().
# Reviewer-role + reviewing → t3:reviewer; author-role + coding →
# t3:orchestrator (it chains coder → tester → reviewer → shipper).
_SUBAGENT_BY_PHASE: dict[tuple[str, str], str] = {
    (Ticket.Role.REVIEWER, "reviewing"): "t3:reviewer",
    (Ticket.Role.AUTHOR, "coding"): "t3:orchestrator",
}


def _subagent_for(task: Task) -> str:
    ticket = task.ticket
    return _SUBAGENT_BY_PHASE.get((ticket.role, task.phase), "")


def _dispatchable_q() -> Q:
    """DB-side mirror of ``_subagent_for`` for the atomic claim filter.

    ``Q`` matching exactly the (ticket.role, task.phase) pairs that have a
    registered subagent, so the atomic claim restricts to dispatchable
    tasks (one source of truth).
    """
    q = Q(pk__in=[])  # matches nothing; OR-folded below
    for role, phase in _SUBAGENT_BY_PHASE:
        q |= Q(ticket__role=role, phase=phase)
    return q


def _task_to_dict(task: Task) -> dict[str, Any]:
    ticket = task.ticket
    return {
        "task_id": int(task.pk),
        "ticket_id": int(ticket.pk),
        "phase": task.phase,
        "subagent": _subagent_for(task),
        "execution_reason": task.execution_reason,
        "issue_url": ticket.issue_url,
        "ticket_role": ticket.role,
        "ticket_state": ticket.state,
        "ticket_extra": ticket.extra or {},
    }


class Command(TyperCommand):
    @command(name="pending-spawn")
    def pending_spawn(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the pending list as JSON instead of a table."),
        ] = False,
    ) -> None:
        """List pending Tasks the ``/loop`` slot should spawn in-session.

        Tasks are returned in FIFO order (oldest pending first). The
        ``subagent`` field tells the slot which subagent_type to pass
        to its ``Agent`` tool; an empty string means the role+phase pair
        has no registered subagent (operator triage).
        """
        pending = Task.objects.filter(status=Task.Status.PENDING).select_related("ticket").order_by("pk")
        payload = [_task_to_dict(task) for task in pending if _subagent_for(task)]
        if json_output:
            self.stdout.write(json.dumps(payload, indent=2))
            return
        if not payload:
            self.stdout.write("No pending spawn requests.")
            return
        for entry in payload:
            self.stdout.write(
                f"task={entry['task_id']:<5} subagent={entry['subagent']:<18} "
                f"phase={entry['phase']:<10} url={entry['issue_url']}",
            )

    @command(name="claim-next")
    def claim_next(
        self,
        *,
        claimed_by: Annotated[
            str,
            typer.Option("--claimed-by", help="Worker identifier stored on the claim."),
        ] = "loop-slot",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the claimed dispatch as JSON instead of a table."),
        ] = False,
    ) -> None:
        """Atomically claim the oldest pending dispatchable Task, then emit it.

        #786 (N4): the claim IS the spawn boundary. Delegates to the single
        audited claim path ``Task.objects.claim_next_pending`` (one
        critical section, backend-agnostic conditional UPDATE — correct on
        SQLite, not just Postgres), narrowed to dispatchable (role, phase)
        pairs so a non-dispatchable PENDING task is left untouched for
        operator triage. Two concurrent ticks each claim a *distinct* task
        (or nothing); the slot calls its ``Agent`` tool for the emitted
        already-claimed entry. The previous inline reimplementation (N2)
        and the SQLite-ineffective ``skip_locked`` (B1) are gone.
        """
        task = Task.objects.claim_next_pending(
            claimed_by=claimed_by,
            extra_filter=_dispatchable_q(),
        )
        payload: list[dict[str, Any]] = [_task_to_dict(task)] if task is not None else []

        if json_output:
            self.stdout.write(json.dumps(payload, indent=2))
            return
        if not payload:
            self.stdout.write("No pending spawn requests.")
            return
        entry = payload[0]
        self.stdout.write(
            f"Claimed task={entry['task_id']} subagent={entry['subagent']} "
            f"phase={entry['phase']} url={entry['issue_url']}",
        )

    @command(name="spawn-claim")
    def spawn_claim(
        self,
        task_id: Annotated[int, typer.Argument(help="Task PK to claim.")],
        *,
        claimed_by: Annotated[
            str,
            typer.Option("--claimed-by", help="Worker identifier stored on the claim."),
        ] = "loop-slot",
    ) -> None:
        """Mark the Task as claimed so the next tick doesn't surface it.

        Called by the ``/loop`` slot immediately after it calls ``Agent``
        for the entry. The Task transitions to ``completed`` when the
        spawned sub-agent reports back (via the existing TaskAttempt
        flow) — claiming is the boundary, not the finish.
        """
        try:
            task = Task.objects.get(pk=task_id)
        except ObjectDoesNotExist:
            self.stderr.write(f"Task {task_id} not found.")
            raise SystemExit(1) from None
        try:
            task.claim(claimed_by=claimed_by)
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(f"Cannot claim task {task_id}: {exc}")
            raise SystemExit(1) from None
        self.stdout.write(f"Claimed task {task_id} for {claimed_by}.")
