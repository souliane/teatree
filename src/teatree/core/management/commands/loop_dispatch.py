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
