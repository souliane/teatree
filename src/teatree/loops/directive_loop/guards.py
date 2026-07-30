"""The unconditional guard chains — the code half of the QUADRUPLE-OFF (north-star PR-7).

Every directive-loop tick runs a guard chain before it touches a directive. Both chains
are fail-closed and ordered so the first (most fundamental) refusal wins, mirroring
:mod:`teatree.loops.outer_loop.guards` and REUSING its probes verbatim (self-modification
must never run under an unproven merge supervisor):

G1 flag: ``directive_loop_enabled`` off ⇒ ``directive_loop_disabled``. G1b score:
``factory_score_enabled`` off ⇒ ``factory_score_disabled`` (the admission baseline +
no-regression evidence are meaningless without the metric, and its snapshot table must
stay empty while off). G2 critic-live: the critic gate is not a proven live merge
supervisor (< ``MIN_CRITIC_SAMPLE`` verdicts) ⇒ ``critic_not_live`` — never let self-mod
merge under a supervisor that cannot block. G3 signal-trust: any factory signal reports
``instrumentation_gap`` ⇒ ``signal_untrusted`` (never verify against an untrustworthy
score). G4 budget: the shared self-improve budget precheck refuses ⇒ ``budget:<reason>``.

G1b and G2 are scoped to the arc whose risk they actually bound (#3643, #3649).
:func:`evaluate_execution_guards` runs the full G1→G1b→G2→G3→G4 chain for the
post-admission arc, where the loop changes config, merges a mechanism, and measures
against the score. :func:`evaluate_intake_guards` drops BOTH for the pre-admission arc,
which only interprets owner intent and then STOPS at the structural human ratify gate
(:meth:`~teatree.core.models.directive.Directive.admit` raises without a consumed,
answered ratify question).

Why the narrowing is not a weakening: G2's precondition — a proven live merge
supervisor — protects the act of MERGING an unsupervised self-modification. The intake
arc merges nothing and writes no config: its only effects are an interpreter dispatch
row, a question record, and FSM state flips that all terminate at the human gate. Its
safety baseline is that gate, which is structural rather than probed, so it cannot be
satisfied-by-accident the way a guard chain can. Requiring the critic there gated owner
intent behind an unshipped subsystem — 37 captured directives went uninterpreted for
weeks — while protecting nothing the ratify gate does not already protect. Every
effectful step stays behind BOTH the human gate and the full execution chain.

Nothing here mutates state, so every guard is table-tested; a refusal returns a typed
:class:`~teatree.loops.outer_loop.guards.GuardVerdict` and the tick is a total no-op.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from teatree.loops.outer_loop.guards import (
    GuardSeams,
    GuardVerdict,
    precheck_budget,
    probe_critic_liveness,
    probe_signal_trust,
)

FLAG_OFF = "directive_loop_disabled"
SCORE_OFF = "factory_score_disabled"
CRITIC_NOT_LIVE = "critic_not_live"
SIGNAL_UNTRUSTED = "signal_untrusted"
BUDGET = "budget"


class DirectiveLoopSettings(Protocol):
    """The effective-settings surface the directive loop reads.

    Structural, so a real ``UserSettings`` and a test ``SimpleNamespace`` both satisfy
    it without an explicit inheritance.
    """

    directive_loop_enabled: bool
    factory_score_enabled: bool
    directive_verify_days: int
    directive_intake_per_tick: int


def evaluate_intake_guards(
    *,
    settings: DirectiveLoopSettings,
    seams: GuardSeams | None = None,
    overlay: str = "",
    now: datetime | None = None,
) -> GuardVerdict:
    """Run G1→G3→G4 for the pre-admission arc; return the first refusal, else allow."""
    return _evaluate(settings=settings, seams=seams, overlay=overlay, now=now, gates=_INTAKE_GATES)


def evaluate_execution_guards(
    *,
    settings: DirectiveLoopSettings,
    seams: GuardSeams | None = None,
    overlay: str = "",
    now: datetime | None = None,
) -> GuardVerdict:
    """Run G1→G1b→G2→G3→G4 for the post-admission arc; first refusal wins, else allow."""
    return _evaluate(settings=settings, seams=seams, overlay=overlay, now=now, gates=_EXECUTION_GATES)


@dataclass(frozen=True, slots=True)
class _ArcGates:
    """Which arc-scoped guards apply — the only difference between the two chains."""

    score: bool
    critic: bool


_INTAKE_GATES = _ArcGates(score=False, critic=False)
_EXECUTION_GATES = _ArcGates(score=True, critic=True)


def _evaluate(
    *,
    settings: DirectiveLoopSettings,
    seams: GuardSeams | None,
    overlay: str,
    now: datetime | None,
    gates: _ArcGates,
) -> GuardVerdict:
    resolved = seams or GuardSeams()
    if not settings.directive_loop_enabled:
        return GuardVerdict.refuse(FLAG_OFF)
    if gates.score and not settings.factory_score_enabled:
        return GuardVerdict.refuse(SCORE_OFF)
    if gates.critic and not (resolved.critic_probe or probe_critic_liveness)().live:
        return GuardVerdict.refuse(CRITIC_NOT_LIVE)
    trust = probe_signal_trust(overlay=overlay, now=now, report=resolved.signal_report)
    if not trust.trusted:
        return GuardVerdict.refuse(SIGNAL_UNTRUSTED)
    resolved_budget = resolved.budget if resolved.budget is not None else precheck_budget()
    if not resolved_budget.ok:
        return GuardVerdict.refuse(f"{BUDGET}:{resolved_budget.reason}")
    return GuardVerdict.allow()
