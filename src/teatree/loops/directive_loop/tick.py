"""One directive-loop tick — guard chain, then advance one directive one step (north-star PR-7).

:func:`run_tick` is the whole behaviour and structurally mirrors
:func:`teatree.loops.outer_loop.tick.run_tick`. It ALWAYS runs an unconditional
guard chain first; a refusal returns a typed no-op result with zero mutation (the
QUADRUPLE-OFF flag-off parity property) and is LOGGED at warning level, so a refused
tick is never indistinguishable from an idle one (#3643). When the chain allows, it
advances the OLDEST active directive exactly one FSM step:

    CAPTURED       → arm the headless interpreter (idempotent dispatch)
    CLARIFYING     → re-interpret once every clarify question is answered, else wait
    INTERPRETED    → ask the human to ratify the sketch
    RATIFY_PENDING → admit on an approved answer / reject on a denial / wait
    ADMITTED       → build the mechanism (setting_policy_gate), or skip to configure for
        activation_only, + snapshot the admission baseline
    IMPLEMENTING   → wait for the mechanism ticket to merge, then configure
    CONFIGURING    → apply the ratified overlay activation (byte-identical), then verify
    VERIFYING      → decide keep/revert once the horizon elapses, else wait
    REVERT_PENDING → ask the human to revert (once), then wait for resolve-revert

The guard chain is arc-scoped: the pre-admission INTAKE arc runs
:func:`~teatree.loops.directive_loop.guards.evaluate_intake_guards`, and the
post-admission EXECUTION arc — consulted only once a directive is past the human ratify
gate — runs the score-requiring
:func:`~teatree.loops.directive_loop.guards.evaluate_execution_guards`.

Every dependency the tick reads is injectable (:class:`TickSeams`) so the whole
pipeline is exercisable without a live critic, a real merge, a real pytest run, or a
real clock; the production cron passes none and the real probes apply.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.models import Directive
from teatree.core.models.ticket import Ticket
from teatree.loops.directive_loop.configure import apply_activation
from teatree.loops.directive_loop.guards import DirectiveLoopSettings, evaluate_execution_guards, evaluate_intake_guards
from teatree.loops.directive_loop.implement import schedule_directive_implementation, skip_directive_implementation
from teatree.loops.directive_loop.interpret import (
    clarifications_answered,
    dispatch_interpretation,
    reinterpret_after_clarification,
)
from teatree.loops.directive_loop.ratify import ask_ratification, try_admit
from teatree.loops.directive_loop.revert import ask_revert
from teatree.loops.directive_loop.verify import (
    VerifySeams,
    horizon_elapsed,
    rollback_and_request_revert,
    verify_and_decide,
)
from teatree.loops.outer_loop.guards import GuardSeams

logger = logging.getLogger(__name__)

_ACTIVATION_ONLY = "activation_only"


@dataclass(frozen=True, slots=True)
class TickSeams:
    """Injectable seams for one tick — the guard seams plus the merge/verify seams.

    All-default in production; tests supply fakes to drive the pipeline without a live
    critic, a real merge, a real pytest run, or a real clock.
    """

    guards: GuardSeams = field(default_factory=GuardSeams)
    merged_probe: Callable[[Directive], bool] | None = None
    verify_seams: VerifySeams | None = None


@dataclass(frozen=True, slots=True)
class DirectiveTickResult:
    """The typed outcome of one tick — the loop's liveness evidence."""

    action: str
    reason: str = ""
    directive_id: int | None = None


def _ticket_merged(directive: Directive) -> bool:
    """Whether the directive's mechanism ticket has reached a merged-or-past state."""
    merged_states = {Ticket.State.MERGED, Ticket.State.RETROSPECTED, Ticket.State.DELIVERED}
    ticket = directive.ticket
    return ticket is not None and ticket.state in merged_states


def _is_activation_only(directive: Directive) -> bool:
    sketch = directive.sketch
    return sketch is not None and sketch.kind == _ACTIVATION_ONLY


def run_tick(
    *,
    overlay: str = "",
    now: datetime | None = None,
    settings: DirectiveLoopSettings | None = None,
    seams: TickSeams | None = None,
) -> DirectiveTickResult:
    """Run one tick: arc-scoped guard chain, then advance the oldest active directive one step."""
    resolved_seams = seams or TickSeams()
    resolved_settings = settings if settings is not None else get_effective_settings(overlay or None)
    intake_verdict = evaluate_intake_guards(
        settings=resolved_settings, seams=resolved_seams.guards, overlay=overlay, now=now
    )
    if not intake_verdict.ok:
        return _refused(intake_verdict.reason)

    directive = Directive.objects.active().order_by("created_at", "pk").first()
    if directive is None:
        return DirectiveTickResult(action="idle", reason="no_active_directive")

    intake = _advance_intake(directive)
    if intake is not None:
        return intake

    execution_verdict = evaluate_execution_guards(
        settings=resolved_settings, seams=resolved_seams.guards, overlay=overlay, now=now
    )
    if not execution_verdict.ok:
        return _refused(execution_verdict.reason, directive_id=directive.pk)
    return _advance_execution(directive, resolved_settings, now=now, seams=resolved_seams)


def _refused(reason: str, *, directive_id: int | None = None) -> DirectiveTickResult:
    """Refuse the tick and LOG it — a silent refusal reads exactly like an idle tick."""
    logger.warning("directive_loop tick refused: %s (directive=%s)", reason, directive_id)
    return DirectiveTickResult(action="refused", reason=reason, directive_id=directive_id)


def _advance_intake(directive: Directive) -> DirectiveTickResult | None:
    """The pre-admission arc (interpret → clarify → ratify → admit); ``None`` past ADMITTED."""
    state = Directive.State
    if directive.state == state.CAPTURED:
        dispatch_interpretation(directive)
        return DirectiveTickResult(action="interpret_dispatched", directive_id=directive.pk)
    if directive.state == state.CLARIFYING:
        return _advance_clarifying(directive)
    if directive.state == state.INTERPRETED:
        ask_ratification(directive)
        return DirectiveTickResult(action="ratify_asked", directive_id=directive.pk)
    if directive.state == state.RATIFY_PENDING:
        return DirectiveTickResult(action=try_admit(directive), directive_id=directive.pk)
    return None


def _advance_execution(
    directive: Directive, settings: DirectiveLoopSettings, *, now: datetime | None, seams: TickSeams
) -> DirectiveTickResult:
    """The post-admission arc (implement → configure → verify → revert); one step per tick."""
    state = Directive.State
    if directive.state == state.ADMITTED:
        return _advance_admitted(directive)
    if directive.state == state.IMPLEMENTING:
        return _advance_implementing(directive, merged_probe=seams.merged_probe)
    if directive.state == state.CONFIGURING:
        return _advance_configuring(directive, now=now)
    if directive.state == state.VERIFYING:
        return _advance_verifying(directive, settings, now=now, verify_seams=seams.verify_seams)
    return _advance_revert_pending(directive)


def _advance_clarifying(directive: Directive) -> DirectiveTickResult:
    """Re-interpret once every clarify question is answered; else wait on the human."""
    if not clarifications_answered(directive):
        return DirectiveTickResult(action="waiting", reason="awaiting_clarification", directive_id=directive.pk)
    reinterpret_after_clarification(directive)
    return DirectiveTickResult(action="reinterpret_dispatched", directive_id=directive.pk)


def _advance_admitted(directive: Directive) -> DirectiveTickResult:
    """Skip to configure for ``activation_only``; else build the mechanism (IMPLEMENTING)."""
    if _is_activation_only(directive):
        skip_directive_implementation(directive)
        return DirectiveTickResult(action="configuring", directive_id=directive.pk)
    schedule_directive_implementation(directive)
    return DirectiveTickResult(action="implementing", directive_id=directive.pk)


def _advance_implementing(
    directive: Directive, *, merged_probe: Callable[[Directive], bool] | None
) -> DirectiveTickResult:
    """Move to CONFIGURING once the mechanism ticket has merged; else wait."""
    if not (merged_probe or _ticket_merged)(directive):
        return DirectiveTickResult(action="waiting", reason="implement_in_flight", directive_id=directive.pk)
    directive.begin_configuring()
    return DirectiveTickResult(action="configuring", directive_id=directive.pk)


def _advance_configuring(directive: Directive, *, now: datetime | None) -> DirectiveTickResult:
    """Apply the ratified overlay activation, then arm the verify horizon.

    A persistent refusal (a drift or read-back mismatch — a genuine anomaly, since the
    setting_key is validated at record time and the activation is derived from the
    ratified sketch) is deterministic and would never self-heal on a retry, so it
    ESCALATES to a human-asked revert rather than soft-locking the slot in perpetual
    ``waiting``. An empty-scope global mechanism configures as a no-op success and
    advances normally.
    """
    result = apply_activation(directive)
    if result.applied:
        directive.begin_verifying(now=now)
        return DirectiveTickResult(action="verifying", directive_id=directive.pk)
    rollback_and_request_revert(directive, reason=f"configure refused: {result.reason}")
    return DirectiveTickResult(action="revert_pending", directive_id=directive.pk)


def _advance_verifying(
    directive: Directive, settings: DirectiveLoopSettings, *, now: datetime | None, verify_seams: VerifySeams | None
) -> DirectiveTickResult:
    """Decide keep/revert once the verify horizon elapses; else wait."""
    moment = now or timezone.now()
    if not horizon_elapsed(directive, verify_days=settings.directive_verify_days, now=moment):
        return DirectiveTickResult(action="waiting", reason="horizon_not_elapsed", directive_id=directive.pk)
    decision = verify_and_decide(directive, now=now, seams=verify_seams)
    return DirectiveTickResult(
        action="fulfilled" if decision.fulfilled else "revert_pending", directive_id=directive.pk
    )


def _advance_revert_pending(directive: Directive) -> DirectiveTickResult:
    """Ask the human to revert (once), then wait for `t3 directive resolve-revert`."""
    if directive.revert_question is None:
        ask_revert(directive)
        return DirectiveTickResult(action="revert_asked", directive_id=directive.pk)
    return DirectiveTickResult(action="waiting", reason="awaiting_human_revert", directive_id=directive.pk)
