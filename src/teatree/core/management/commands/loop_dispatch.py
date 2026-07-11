"""``manage.py loop_dispatch`` — read & claim pending agent dispatches.

The DB is the dispatch queue: when ``run_tick`` produces a
``kind="agent"`` action, ``teatree.loop.persistence`` creates a Ticket
+ Task row. The ``/loop`` slot's session reads pending Tasks via
``pending-spawn``, calls its ``Agent`` tool once per entry, then claims
each via ``spawn-claim`` so the next tick doesn't see them as pending.
"""

import contextlib
import json
import logging
from typing import Annotated, Any

import typer
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django_typer.management import TyperCommand, command

from teatree.config import cadence_seconds
from teatree.core.modelkit.phases import resolve_fanout_directive, subagent_for_phase
from teatree.core.models import Task
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.loop.admit_budget import read_admit_budget
from teatree.loop.dispatch_gates import spawn_display_name
from teatree.loop.statusline import default_path

logger = logging.getLogger(__name__)


def _subagent_for(task: Task) -> str:
    return subagent_for_phase(task.ticket.role, task.phase)


def _dispatchable_q() -> Q:
    """The in-session claim filter — the ``dispatchable_q`` SSOT narrowed to INTERACTIVE (#6).

    ``Task.dispatchable_q()`` is the single source of truth (role/phase pairs
    with a registered sub-agent AND not under a live #2104 external-delivery
    lease, #2217). Sharing it means ``claim-next`` and ``pending-spawn`` honour
    the external-delivery exclusion the same as the ``orchestrate`` planner — the
    #2218 "fix landed on one side" recurrence dies.

    The in-session ``/loop`` claims only INTERACTIVE tasks: under a headless
    ``agent_runtime`` a loop-dispatched phase task is HEADLESS and owned by the
    headless lane (``execute_headless_task``), so AND-ing ``execution_target ==
    INTERACTIVE`` keeps the two lanes disjoint — the same task is never claimed
    in-session AND run headless. The admit-budget count deliberately does NOT
    apply this narrowing (``_admit_budget_exhausted``), so a headless claim in
    flight still consumes the boost budget.
    """
    return Task.dispatchable_q() & Q(execution_target=Task.ExecutionTarget.INTERACTIVE)


def _admit_budget_exhausted() -> bool:
    """True when the orchestrate admit budget is hit — refuse the marginal claim (#1796).

    The reconciled fan-out persists a per-tick admit *ceiling* to the tick-meta
    sidecar (the read-only ``orchestrate_phase`` planner). This live claimer
    reads it and refuses once the standing in-flight CLAIMED dispatchable WIP
    has reached the ceiling, so claimed ≡ spawned and the orphan window is
    closed. The CAS still serializes the marginal claim; this gate only decides
    *whether* to attempt it.

    **Fail open to UNCLAMPED** (returns ``False``) when the budget is absent
    (medium / toggle-off — today's throughput), stale (> TTL, a dead loop wrote
    it), or any read error — a dead loop must never wrongly clamp live dispatch.

    #6: the in-flight count runs over ``Task.dispatchable_q()`` WITHOUT the
    ``execution_target == INTERACTIVE`` narrowing — the SAME filter set the
    ``orchestrate`` planner used to compute the target. A HEADLESS loop-dispatched
    phase task in flight (under a headless ``agent_runtime``) therefore consumes
    the boost budget too; the pre-fix gate counted only INTERACTIVE in-flight and
    overshot ``N`` whenever headless workers were running.
    """
    try:
        budget = read_admit_budget(statusline_path=default_path(), cadence_seconds=cadence_seconds())
    except Exception:  # noqa: BLE001 — a budget-read failure degrades to no-budget
        return False
    if budget is None:
        return False
    return Task.objects.in_flight_claimed_count(Task.dispatchable_q()) >= budget


def _task_to_dict(task: Task) -> dict[str, Any]:
    ticket = task.ticket
    model, skill_bundle = _resolve_model_and_bundle(task)
    subagent = _subagent_for(task)
    return {
        "task_id": int(task.pk),
        "ticket_id": int(ticket.pk),
        "phase": task.phase,
        "subagent": subagent,
        # PR-12: the type-prefixed display name (``t3-<type>-<id>``) the /loop
        # slot passes to its Agent tool, so every spawn is attributable at a
        # glance and never an anonymous general-purpose one.
        "display_name": spawn_display_name(subagent, int(task.pk)),
        "execution_reason": task.execution_reason,
        "issue_url": ticket.issue_url,
        "ticket_role": ticket.role,
        "ticket_state": ticket.state,
        "ticket_extra": ticket.extra or {},
        # Model tier + skill bundle resolved in LOOP scope (not inside a
        # detached headless-SDK run) so the in-session ``/loop`` slot passes
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
        # Session that took the claim (empty until the worker session is known),
        # orthogonal to the role-label ``claimed_by``.
        "claimed_by_session": task.claimed_by_session,
    }


def _resolve_fanout_directive(task: Task) -> str:
    """Resolve the fan-out directive for a dispatch, loop-side; empty by default.

    The ``[agent]`` config is read here (the local import keeps ``teatree.core``
    free of a top-level ``teatree.config.agent_spawn`` dependency edge — core is
    the lower layer, same pattern as ``_resolve_model_and_bundle``'s local import).
    ``resolve_agent_config`` itself fails-to-defaults on a missing/malformed
    file (returning ``AgentConfig()`` with an empty ``phase_fanout``), so a
    config read problem degrades to ``""`` without blocking the dispatch. The
    chokepoint ``resolve_fanout_directive`` returns ``""`` when the pair has no
    registered fan-out OR no opt-in — empty until a pair is opted in. An
    explicitly out-of-range ``N`` raises ``ValueError`` (fail-loud), surfacing
    the misconfiguration rather than silently dropping it.
    """
    from teatree.config.agent_spawn import resolve_agent_config  # noqa: PLC0415 — deferred: keep core import-light

    return resolve_fanout_directive(task.ticket.role, task.phase, resolve_agent_config())


def _resolve_model_and_bundle(task: Task) -> tuple[str | None, list[str]]:
    """Resolve the spawn model tier and skill bundle for a dispatch, loop-side.

    Moved out of the detached headless-SDK run (``run_headless``) so the
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
    from teatree.core.modelkit.phases import normalize_phase  # noqa: PLC0415

    skill_bundle = _resolve_skill_bundle(task)
    session_id = task.session.agent_id if task.session_id else None  # ty: ignore[unresolved-attribute]
    model = resolve_spawn_model(
        normalize_phase(task.phase),
        skills=skill_bundle,
        session_id=session_id or None,
        task_id=int(task.pk),
    )
    return model, skill_bundle


def _resolve_skill_bundle(task: Task) -> list[str]:
    """Resolve the loaded skill bundle for *task*; empty on any discovery failure.

    Resolves the overlay and the framework/detection cwd from the TASK's ticket
    (its overlay + its worktree, PR-12) — never the orchestrator's ambient cwd,
    which is the loop's clone rather than the ticket's checkout. Imports
    ``resolve_skill_bundle`` locally to keep ``teatree.core`` free of a top-level
    ``teatree.agents`` dependency edge (core is the lower layer).
    """
    from teatree.agents.skill_bundle import resolve_skill_bundle  # noqa: PLC0415

    try:
        from teatree.core.overlay_loader import get_overlay_for_ticket  # noqa: PLC0415

        overlay_skill_metadata = get_overlay_for_ticket(task.ticket).metadata.get_skill_metadata()
        return resolve_skill_bundle(
            phase=task.phase,
            overlay_skill_metadata=overlay_skill_metadata,
            worktree_path=dispatch_worktree_path(task.ticket),
        )
    except Exception:  # noqa: BLE001 — a failure degrades to no candidates
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
        claimable_only: Annotated[
            bool,
            typer.Option(
                "--claimable-only",
                help="Report work ONLY when a claim could land (honour the admit budget).",
            ),
        ] = False,
    ) -> None:
        """List pending Tasks the ``/loop`` slot should spawn in-session.

        Tasks are returned in FIFO order (oldest pending first), filtered through
        the SAME ``_dispatchable_q()`` the atomic ``claim-next`` uses (#6) — the
        ``Task.dispatchable_q`` SSOT narrowed to INTERACTIVE — so the
        in-session preview cannot drift from the claim: a non-dispatchable pair,
        a HEADLESS task owned by the headless lane, and a ticket under a live
        #2104 external-delivery lease are all excluded here exactly as they are
        at claim time. The ``subagent`` field tells the slot which subagent_type
        to pass to its ``Agent`` tool; the ``display_name`` field
        (``t3-<type>-<id>``, PR-12) is the Agent tool ``description`` the slot
        passes, so every spawn is attributable and type-prefixed.

        ``--claimable-only`` (TODO #100) applies the SAME admit-budget gate
        ``claim-next`` applies, so the probe answers "is there a unit a claim
        could actually take?" rather than "is there any dispatchable PENDING
        row?". The Stop-hook self-pump uses it: without the gate the probe
        reports an un-advanceable unit (one held back by a full in-flight
        budget) forever, so the self-pump re-offers a unit ``claim-next``
        would always refuse — it never advances or stops. The gate
        fails OPEN (unclamped) on an absent / stale / unreadable budget,
        identical to the claimer.
        """
        if claimable_only and _admit_budget_exhausted():
            payload: list[dict[str, Any]] = []
        else:
            pending = (
                Task.objects.filter(status=Task.Status.PENDING)
                .filter(_dispatchable_q())
                .select_related("ticket")
                .order_by("pk")
            )
            payload = [_task_to_dict(task) for task in pending]
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
        claimed_by_session: Annotated[
            str | None,
            typer.Option(
                "--claimed-by-session",
                help="Worker session id stored on the claim (defaults to the active session, empty when none).",
            ),
        ] = None,
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

        ``--claimed-by-session`` attributes the claim to the worker session
        that took it (#1917). Unset, it resolves to ``current_session_id()``
        (empty when no session is resolvable); it rides the SET clause of the
        claim only, never the CAS predicate, so the claim semantics are
        unchanged.

        #1796 (WI-1): before the CAS, honour the orchestrate admit-budget
        ceiling the read-only ``orchestrate_phase`` planner persists to the
        tick-meta sidecar. When the standing in-flight CLAIMED dispatchable WIP
        has reached the ceiling, refuse with the existing empty no-work payload
        (exactly today's no-work path) so claimed ≡ spawned and the loop never
        orphans a claim. Absence / staleness of the budget is UNCLAMPED — the
        default ``medium`` / toggle-off throughput is byte-identical.
        """
        from teatree.core.session_identity import current_session_id  # noqa: PLC0415

        # Reclaim a dead session's orphan BEFORE claiming (#652): a unit whose
        # owner stopped heartbeating (its lease lapsed) is returned to PENDING so
        # THIS healthy session's claim picks it up. The full loop tick already
        # runs this via ``_reap_stale_task_claims``; the standalone ``claim-next``
        # entry (Stop-hook self-pump / slack-answer cycle) did not, so a dead
        # session's unit stalled CLAIMED until some other session happened to run
        # a full tick. ``reclaim_orphaned_claims`` is the budget-aware (#2009)
        # CAS — a no-op when nothing is stale, and it leaves a still-live lease
        # untouched (the WHERE re-asserts ``lease_expires_at < now``). Best-effort
        # so a DB-blocked harness still claims (parity with the tick sweep).
        with contextlib.suppress(RuntimeError):
            Task.objects.reclaim_orphaned_claims()

        session = current_session_id() if claimed_by_session is None else claimed_by_session
        if _admit_budget_exhausted():
            task = None
        else:
            from teatree.loop.queue_drain import admission_claim_order  # noqa: PLC0415

            task = Task.objects.claim_next_pending(
                claimed_by=claimed_by,
                claimed_by_session=session,
                extra_filter=_dispatchable_q(),
                ordering=admission_claim_order(),
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
        except Exception as exc:  # noqa: BLE001 — a claim failure surfaces as a clean SystemExit, never a traceback
            self.stderr.write(f"Cannot claim task {task_id}: {exc}")
            raise SystemExit(1) from None
        self.stdout.write(f"Claimed task {task_id} for {claimed_by}.")
