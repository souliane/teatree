"""``manage.py loop_dispatch`` — read & atomically claim pending agent dispatches."""

import contextlib
import logging
from typing import IO, Annotated, Any, cast

import typer
from django_typer.management import TyperCommand, command, initialize

from teatree.config import UserSettings, cadence_seconds, get_effective_settings
from teatree.core.machine_output import emit
from teatree.core.managers_task_claim import redispatch_window
from teatree.core.modelkit.phases import resolve_fanout_directive, subagent_for_phase
from teatree.core.models import Task
from teatree.core.models.task_claim import claim_generation
from teatree.core.models.task_handoff import dispatch_reason
from teatree.core.worktree.clone_paths import dispatch_detection_root
from teatree.loop.admission import governor_verdict
from teatree.loop.admit_budget import read_admit_budget
from teatree.loop.dispatch_gates import spawn_display_name
from teatree.loop.statusline import default_path

logger = logging.getLogger(__name__)


def _subagent_for(task: Task) -> str:
    return subagent_for_phase(task.ticket.role, task.phase)


def _admit_budget_exhausted() -> bool:
    """True when the orchestrate admit budget is hit — refuse the marginal claim (#1796).

    The reconciled fan-out persists a per-tick admit *ceiling* to the tick-meta
    sidecar (the read-only ``orchestrate_phase`` planner). This live claimer
    reads it and refuses once the standing in-flight CLAIMED dispatchable WIP
    has reached the ceiling, so claimed ≡ spawned and the orphan window is
    closed. The CAS still serializes the marginal claim; this gate only decides
    *whether* to attempt it.

    An absent budget (medium / toggle-off), a stale one (> TTL, a dead loop wrote it),
    or any read error degrades to NO SIDECAR CLAMP — a dead loop must never wrongly
    clamp live dispatch. That is not the same as unclamped: the governor still supplies
    a ceiling of its own below (#4097), so the budget's absence removes the operator's
    bound, not the bound.

    #3644: this is the admission chokepoint, so it is where the adaptive governor is
    ASKED (event-driven, at the decision point). The governor's verdict either denies
    outright — token quota first, machine load second, both logged — or supplies a live
    ceiling; the sidecar budget becomes the operator's upper BOUND on it. Only a ``None``
    VERDICT (kill-switch off, or a failed probe) leaves the pre-governor behaviour
    byte-for-byte intact, and then an absent budget really is unclamped.

    #6: the in-flight count runs over ``Task.dispatchable_q()`` — the SAME filter
    set the ``orchestrate`` planner used to compute the target, so every
    loop-dispatched phase task in flight consumes the boost budget.
    """
    try:
        budget = read_admit_budget(statusline_path=default_path(), cadence_seconds=cadence_seconds())
    except Exception:  # noqa: BLE001 — a budget-read failure degrades to no-budget
        return False
    governed = governor_verdict(statusline_path=default_path(), static_ceiling=budget)
    if governed is not None:
        if not governed.admit:
            return True
        budget = governed.ceiling
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
        "execution_reason": dispatch_reason(task),
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
        # Per-phase fan-out directive (teatree#2229), resolved from the registry
        # beside model/skill_bundle. Unregistered pairs render an empty string.
        "fanout_directive": _resolve_fanout_directive(task),
        # Session that took the claim (empty until the worker session is known),
        # orthogonal to the role-label ``claimed_by``.
        "claimed_by_session": task.claimed_by_session,
        # The generation this claim minted. The slot hands it straight back to
        # ``tasks record-attempt --claim-token``, which is what stops a run whose
        # lease lapsed and was re-offered mid-flight from recording its outcome
        # onto the generation the next tick is executing.
        "claim_token": claim_generation(task),
    }


def _interactive_claim_lease_seconds() -> int:
    """How long an in-session claim holds WITHOUT a heartbeat.

    The ``/loop`` slot's ``Agent``-tool sub-agent runs inside the owner's own
    single-threaded session, so nothing can renew the lease while it works —
    ``renew_lease``'s only production caller is the detached headless heartbeat.
    The initial lease is therefore the WHOLE budget, and ``Task.claim``'s 300s
    default reclaims and re-offers every dispatch slower than five minutes, which
    is most of them. The instant a unit is genuinely presumed runaway is already
    named once, by the watchdog runtime ceiling, so the lease is that same instant
    rather than a second number to keep in sync; a disabled (``0``) ceiling means
    the watchdog will not judge a run, never that a lease may not expire, so the
    shipped ceiling stands in.
    """
    return get_effective_settings().watchdog_max_runtime_seconds or UserSettings().watchdog_max_runtime_seconds


def _resolve_fanout_directive(task: Task) -> str:
    """Resolve the fan-out directive for a dispatch from the phase registry."""
    return resolve_fanout_directive(task.ticket.role, task.phase)


def _resolve_model_and_bundle(task: Task) -> tuple[str | None, list[str]]:
    """Resolve the spawn model tier and skill bundle for a dispatch, loop-side.

    Moved out of the detached agent run (``run_agent``) so the ``/loop`` slot
    resolves them once at claim time and threads them into the sub-agent it
    spawns. The skill bundle is resolved FIRST so
    the model is the most-capable-wins floor merge of the phase tier and the
    per-skill ``[agent.skill_models]`` floors of the bundle's skills
    (``resolve_spawn_model``). MODEL only — no effort is threaded into the
    per-sub-agent dispatch (effort is a session-wide pin on the loop spawn; the
    Agent tool has no effort param). Overlay/skill discovery
    failures degrade to an empty bundle so a dispatch is never blocked on
    resolution — the model then collapses to the phase tier and the slot falls
    back to base skills.

    The task's session id + pk are threaded into ``resolve_spawn_model`` so a
    situational honesty-critical escalation (teatree#2263) can raise a
    verification spawn to the most-honest model. Both default to absent on a
    session-less task → byte-identical to today when no escalation is active.
    """
    from teatree.agents.model_tiering import resolve_spawn_model  # noqa: PLC0415 — deferred: keeps command import light
    from teatree.core.modelkit.phases import normalize_phase  # noqa: PLC0415 — deferred: keeps command import light

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

    Resolves the overlay and the framework/detection root from the TASK's ticket
    (its overlay + its worktree or repo clone, PR-12) — never the orchestrator's
    ambient cwd, which is the loop's clone rather than the ticket's checkout. Imports
    ``resolve_skill_bundle`` locally to keep ``teatree.core`` free of a top-level
    ``teatree.agents`` dependency edge (core is the lower layer).
    """
    from teatree.agents.skill_bundle import resolve_skill_bundle  # noqa: PLC0415 — deferred: keeps command import light

    try:
        from teatree.core.overlay_loader import get_overlay_for_ticket  # noqa: PLC0415 — deferred: lazy command import

        overlay_skill_metadata = get_overlay_for_ticket(task.ticket).metadata.get_skill_metadata()
        return resolve_skill_bundle(
            phase=task.phase,
            overlay_skill_metadata=overlay_skill_metadata,
            detection_root=dispatch_detection_root(task.ticket),
        )
    except Exception:  # noqa: BLE001 — a failure degrades to no candidates
        return []


class Command(TyperCommand):
    @initialize()
    def init(self) -> None:
        """``loop_dispatch`` group root."""

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
        orphans a claim. Absence / staleness of the budget drops the SIDECAR
        clamp — the default ``medium`` / toggle-off throughput is byte-identical
        as far as the sidecar goes — while the governor's own ceiling still
        applies (#4097).
        """
        from teatree.core.session_identity import current_session_id  # noqa: PLC0415 — deferred: lazy command import

        # Reclaim a dead session's orphan BEFORE claiming (#652): a unit whose
        # owner stopped heartbeating (its lease lapsed) is returned to PENDING so
        # THIS healthy session's claim picks it up. The full loop tick already
        # runs this via ``_reap_stale_task_claims``; the standalone ``claim-next``
        # entry did not, so a dead
        # session's unit stalled CLAIMED until some other session happened to run
        # a full tick. ``reclaim_orphaned_claims`` is the budget-aware (#2009)
        # CAS — a no-op when nothing is stale, and it leaves a still-live lease
        # untouched (the WHERE re-asserts ``lease_expires_at < now``). Best-effort
        # so a DB-blocked harness still claims (parity with the tick sweep).
        with contextlib.suppress(RuntimeError), redispatch_window() as refusal:
            if not refusal:
                Task.objects.reclaim_orphaned_claims()

        session = current_session_id() if claimed_by_session is None else claimed_by_session
        if _admit_budget_exhausted():
            task = None
        else:
            from teatree.loop.queue_drain import admission_claim_order  # noqa: PLC0415 — deferred: lazy command import

            task = Task.objects.claim_next_pending(
                claimed_by=claimed_by,
                claimed_by_session=session,
                lease_seconds=_interactive_claim_lease_seconds(),
                extra_filter=Task.dispatchable_q(),
                ordering=admission_claim_order(),
            )
        payload: list[dict[str, Any]] = [_task_to_dict(task)] if task is not None else []

        if not payload:
            human: str | None = "No pending spawn requests."
        else:
            entry = payload[0]
            human = (
                f"Claimed task={entry['task_id']} subagent={entry['subagent']} "
                f"phase={entry['phase']} url={entry['issue_url']} claim_token={entry['claim_token']}"
            )
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )
