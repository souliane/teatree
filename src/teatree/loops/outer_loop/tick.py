"""One outer-loop tick — guard chain, then advance at most one experiment (T4-PR-3).

:func:`run_tick` is the whole behaviour. It ALWAYS runs the unconditional guard
chain first; a refusal returns a typed no-op result with zero mutation (the
flag-off parity property). When the chain allows, it advances the oldest active
experiment exactly one FSM step, or — with no active experiment — proposes a new
one within the admission caps (parking on convergence).

Every dependency the tick reads is injectable so the pipeline is exercisable in
tests without a live critic, a real merge, or a real clock; the production cron
passes none and the real probes apply.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.factory.factory_signals import FactorySignalsReport
from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment
from teatree.core.models.ticket import Ticket
from teatree.loops.outer_loop.guards import CONVERGED, GuardSeams, OuterLoopSettings, admission_verdict, evaluate_guards
from teatree.loops.outer_loop.implement import schedule_experiment_fix
from teatree.loops.outer_loop.measure import arm_measurement, horizon_elapsed, measure_and_decide
from teatree.loops.outer_loop.propose import select_proposal
from teatree.loops.outer_loop.ratify import ask_ratification, try_admit
from teatree.loops.outer_loop.revert import ask_revert
from teatree.loops.outer_loop.score import read_score


@dataclass(frozen=True, slots=True)
class TickSeams:
    """Injectable seams for one tick — the guard seams plus the proposal/merge seams.

    All-default in production; tests supply fakes to drive the pipeline without a
    live critic, a real merge, or a real clock.
    """

    guards: GuardSeams = field(default_factory=GuardSeams)
    propose_report: FactorySignalsReport | None = None
    merged_probe: Callable[[OuterLoopExperiment], bool] | None = None


@dataclass(frozen=True, slots=True)
class OuterLoopTickResult:
    """The typed outcome of one tick — the loop's liveness evidence."""

    action: str
    reason: str = ""
    experiment_id: int | None = None


def _ticket_merged(experiment: OuterLoopExperiment) -> bool:
    """Whether the experiment's synthetic ticket has reached a merged-or-past state."""
    merged_states = {Ticket.State.MERGED, Ticket.State.RETROSPECTED, Ticket.State.DELIVERED}
    ticket = experiment.ticket
    return ticket is not None and ticket.state in merged_states


def run_tick(
    *,
    overlay: str = "",
    now: datetime | None = None,
    settings: OuterLoopSettings | None = None,
    seams: TickSeams | None = None,
) -> OuterLoopTickResult:
    """Run one tick: guard chain, then advance/propose exactly one step."""
    resolved_seams = seams or TickSeams()
    resolved_settings = settings if settings is not None else get_effective_settings(overlay or None)
    verdict = evaluate_guards(settings=resolved_settings, seams=resolved_seams.guards, overlay=overlay, now=now)
    if not verdict.ok:
        return OuterLoopTickResult(action="refused", reason=verdict.reason)

    experiment = OuterLoopExperiment.objects.active(overlay=overlay).order_by("created_at", "pk").first()
    if experiment is None:
        return _maybe_propose(resolved_settings, overlay=overlay, now=now, propose_report=resolved_seams.propose_report)
    return _advance(experiment, resolved_settings, overlay=overlay, now=now, merged_probe=resolved_seams.merged_probe)


def _maybe_propose(
    settings: OuterLoopSettings,
    *,
    overlay: str,
    now: datetime | None,
    propose_report: FactorySignalsReport | None,
) -> OuterLoopTickResult:
    admission = admission_verdict(settings=settings, overlay=overlay, now=now)
    if not admission.ok:
        if admission.reason == CONVERGED:
            _park_converged(overlay)
            return OuterLoopTickResult(action="parked", reason=CONVERGED)
        return OuterLoopTickResult(action="idle", reason=admission.reason)
    candidate = select_proposal(report=propose_report, overlay=overlay, now=now)
    if candidate is None:
        return OuterLoopTickResult(action="idle", reason="no_target_signal")
    baseline = FactoryScoreSnapshot.objects.record_snapshot(read_score(overlay=overlay, now=now), overlay=overlay)
    experiment = OuterLoopExperiment.objects.propose(candidate, overlay=overlay, baseline_snapshot=baseline)
    return OuterLoopTickResult(action="proposed", experiment_id=experiment.pk)


def _advance(
    experiment: OuterLoopExperiment,
    settings: OuterLoopSettings,
    *,
    overlay: str,
    now: datetime | None,
    merged_probe: Callable[[OuterLoopExperiment], bool] | None,
) -> OuterLoopTickResult:
    """Advance the active experiment one FSM step (one branch fires per tick)."""
    state = OuterLoopExperiment.State
    if experiment.state == state.PROPOSED:
        ask_ratification(experiment)
        return OuterLoopTickResult(action="ratify_asked", experiment_id=experiment.pk)
    if experiment.state == state.RATIFY_PENDING:
        return OuterLoopTickResult(action=try_admit(experiment), experiment_id=experiment.pk)
    if experiment.state == state.ADMITTED:
        schedule_experiment_fix(experiment)
        return OuterLoopTickResult(action="implementing", experiment_id=experiment.pk)
    if experiment.state == state.IMPLEMENTING:
        return _advance_implementing(experiment, merged_probe=merged_probe, now=now)
    if experiment.state == state.MEASURING:
        return _advance_measuring(experiment, settings, overlay=overlay, now=now)
    return _advance_revert_pending(experiment)


def _advance_revert_pending(experiment: OuterLoopExperiment) -> OuterLoopTickResult:
    """Ask the human to revert (once), then wait for `t3 outer resolve-revert`."""
    if experiment.revert_question is None:
        ask_revert(experiment)
        return OuterLoopTickResult(action="revert_asked", experiment_id=experiment.pk)
    return OuterLoopTickResult(action="waiting", reason="awaiting_human_revert", experiment_id=experiment.pk)


def _advance_implementing(
    experiment: OuterLoopExperiment,
    *,
    merged_probe: Callable[[OuterLoopExperiment], bool] | None,
    now: datetime | None,
) -> OuterLoopTickResult:
    """Arm the measure horizon once the synthetic ticket has merged; else wait."""
    if not (merged_probe or _ticket_merged)(experiment):
        return OuterLoopTickResult(action="waiting", reason="implement_in_flight", experiment_id=experiment.pk)
    arm_measurement(experiment, now=now)
    return OuterLoopTickResult(action="measuring", experiment_id=experiment.pk)


def _advance_measuring(
    experiment: OuterLoopExperiment,
    settings: OuterLoopSettings,
    *,
    overlay: str,
    now: datetime | None,
) -> OuterLoopTickResult:
    """Decide keep/revert once the horizon elapses; else wait."""
    moment = now or timezone.now()
    if not horizon_elapsed(experiment, measure_days=settings.outer_loop_measure_days, now=moment):
        return OuterLoopTickResult(action="waiting", reason="horizon_not_elapsed", experiment_id=experiment.pk)
    decision = measure_and_decide(experiment, overlay=overlay, now=now)
    return OuterLoopTickResult(action="kept" if decision.keep else "revert_pending", experiment_id=experiment.pk)


def _park_converged(overlay: str) -> None:
    """Record one deduped DeferredQuestion when the loop hits the convergence brake."""
    options_hash = f"outer_loop_converged:{overlay}"
    already = DeferredQuestion.objects.filter(
        options_hash=options_hash, answered_at__isnull=True, dismissed_at__isnull=True
    ).exists()
    if already:
        return
    DeferredQuestion.record(
        "Outer loop hit the convergence brake (consecutive non-kept experiments). "
        "Investigate the proposer/critic before it proposes again.",
        options_hash=options_hash,
    )
