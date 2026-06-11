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

from teatree.core.models import Task
from teatree.core.phases import SUBAGENT_BY_PHASE, phase_spellings, resolve_fanout_directive, subagent_for_phase

# The phase → sub-agent authority is the single canonical map in
# ``teatree.core.phases``. Each author phase dispatches to its OWN agent
# (coding → t3:coder, testing → t3:tester, reviewing → t3:reviewer,
# shipping → t3:shipper); the reviewer-role reviewing entry stays. The
# loop is the per-phase dispatcher, never a single orchestrator that
# chains the phases inline (BLUEPRINT §5.2 / §17.8 invariant 10).
_SUBAGENT_BY_PHASE = SUBAGENT_BY_PHASE


def _subagent_for(task: Task) -> str:
    return subagent_for_phase(task.ticket.role, task.phase)


def _dispatchable_q() -> Q:
    """DB-side mirror of ``_subagent_for`` for the atomic claim filter.

    ``Q`` matching the (ticket.role, task.phase) pairs that have a
    registered subagent, so the atomic claim restricts to dispatchable
    tasks (one source of truth). Phase is matched across every accepted
    spelling (``phase_spellings``) so a short-verb ``code``/``review`` task
    resolves the same as the canonical token ``_subagent_for`` normalizes.
    """
    q = Q(pk__in=[])  # matches nothing; OR-folded below
    for role, phase in _SUBAGENT_BY_PHASE:
        q |= Q(ticket__role=role, phase__in=phase_spellings(phase))
    return q


def _task_to_dict(task: Task) -> dict[str, Any]:
    ticket = task.ticket
    model, skill_bundle = _resolve_model_and_bundle(task)
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
        # Model tier + skill bundle resolved in LOOP scope (not inside a
        # ``claude -p`` subprocess) so the in-session ``/loop`` slot passes
        # ``model`` to its ``Agent`` tool and the ``skill_bundle`` into the
        # sub-agent prompt. ``model`` is ``null`` when the phase inherits the
        # user's default tier (no ``--model`` override).
        "model": model,
        "skill_bundle": skill_bundle,
        # Per-phase fan-out directive (teatree#2229), resolved loop-side beside
        # model/skill_bundle. Empty string by default (no opt-in) → the slot
        # appends nothing → byte-identical to today; the chokepoint renders the
        # directive only when the user opts the ``(role, phase)`` pair in via
        # ``[agent.phase_fanout]``.
        "fanout_directive": _resolve_fanout_directive(task),
    }


def _resolve_fanout_directive(task: Task) -> str:
    """Resolve the fan-out directive for a dispatch, loop-side; empty by default.

    The ``[agent]`` config is read here (the local import keeps ``teatree.core``
    free of a top-level ``teatree.config_agent`` dependency edge — core is the
    lower layer, same pattern as ``_resolve_model_and_bundle``'s local import).
    ``resolve_agent_config`` itself fails-to-defaults on a missing/malformed
    file (returning ``AgentConfig()`` with an empty ``phase_fanout``), so a
    config read problem degrades to ``""`` without blocking the dispatch. The
    chokepoint ``resolve_fanout_directive`` returns ``""`` when the pair has no
    registered fan-out OR no opt-in — empty until a pair is opted in. An
    explicitly out-of-range ``N`` raises ``ValueError`` (fail-loud), surfacing
    the misconfiguration rather than silently dropping it.
    """
    from teatree.config_agent import resolve_agent_config  # noqa: PLC0415

    return resolve_fanout_directive(task.ticket.role, task.phase, resolve_agent_config())


def _resolve_model_and_bundle(task: Task) -> tuple[str | None, list[str]]:
    """Resolve the spawn model tier and skill bundle for a dispatch, loop-side.

    Moved out of the ``claude -p`` subprocess (``run_headless``) so the
    INTERACTIVE ``/loop`` slot resolves them once at claim time and threads
    them into the in-session sub-agent. The skill bundle is resolved FIRST so
    the model is the most-capable-wins floor merge of the phase tier and the
    per-skill ``[agent.skill_models]`` floors of the bundle's skills
    (``resolve_spawn_model``). MODEL only — no effort is threaded into the
    per-sub-agent dispatch (effort is a session-wide pin on the interactive
    loop spawn; the Agent tool has no effort param). Overlay/skill discovery
    failures degrade to an empty bundle so a dispatch is never blocked on
    resolution — the model then collapses to the phase tier and the slot falls
    back to base skills.

    The task's session id + pk are threaded into ``resolve_spawn_model`` so a
    situational honesty-critical escalation (teatree#2263) can raise a
    verification spawn to the most-honest model. Both default to absent on a
    session-less task → byte-identical to today when no escalation is active.
    """
    from teatree.agents.model_tiering import resolve_spawn_model  # noqa: PLC0415
    from teatree.core.phases import normalize_phase  # noqa: PLC0415

    skill_bundle = _resolve_skill_bundle(task.phase)
    session_id = task.session.agent_id if task.session_id else None  # ty: ignore[unresolved-attribute]
    model = resolve_spawn_model(
        normalize_phase(task.phase),
        skills=skill_bundle,
        session_id=session_id or None,
        task_id=int(task.pk),
    )
    return model, skill_bundle


def _resolve_skill_bundle(phase: str) -> list[str]:
    """Resolve the loaded skill bundle for *phase*; empty on any discovery failure.

    Imports ``resolve_skill_bundle`` locally to keep ``teatree.core`` free of a
    top-level ``teatree.agents`` dependency edge (core is the lower layer).
    """
    from teatree.agents.skill_bundle import resolve_skill_bundle  # noqa: PLC0415

    try:
        from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

        overlay_skill_metadata = get_overlay().metadata.get_skill_metadata()
        return resolve_skill_bundle(phase=phase, overlay_skill_metadata=overlay_skill_metadata)
    except Exception:  # noqa: BLE001
        return []


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
