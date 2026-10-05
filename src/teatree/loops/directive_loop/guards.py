"""Guard chains for directive intake and execution."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from teatree.loop.self_improve.budget import precheck_budget
from teatree.loops.shared.guards import GuardSeams, GuardVerdict, probe_signal_trust

SIGNAL_UNTRUSTED = "signal_untrusted"
BUDGET = "budget"


class DirectiveLoopSettings(Protocol):
    """The effective-settings surface the directive loop reads.

    Structural, so a real ``UserSettings`` and a test ``SimpleNamespace`` both satisfy
    it without an explicit inheritance.
    """

    directive_verify_days: int
    directive_intake_per_tick: int


def evaluate_intake_guards(
    *,
    seams: GuardSeams | None = None,
    overlay: str = "",
    now: datetime | None = None,
) -> GuardVerdict:
    """Run G1→G4 for the pre-admission arc; return the first refusal, else allow."""
    return _evaluate(seams=seams, overlay=overlay, now=now, gates=_INTAKE_GATES)


def evaluate_execution_guards(
    *,
    seams: GuardSeams | None = None,
    overlay: str = "",
    now: datetime | None = None,
) -> GuardVerdict:
    """Run the signal-trust and budget guards for the execution arc."""
    return _evaluate(seams=seams, overlay=overlay, now=now, gates=_EXECUTION_GATES)


@dataclass(frozen=True, slots=True)
class _ArcGates:
    """Which arc-scoped guards apply — the only difference between the two chains."""

    signal_trust: bool


_INTAKE_GATES = _ArcGates(signal_trust=False)
_EXECUTION_GATES = _ArcGates(signal_trust=True)


def _evaluate(
    *,
    seams: GuardSeams | None,
    overlay: str,
    now: datetime | None,
    gates: _ArcGates,
) -> GuardVerdict:
    resolved = seams or GuardSeams()
    if gates.signal_trust and not probe_signal_trust(overlay=overlay, now=now, report=resolved.signal_report).trusted:
        return GuardVerdict.refuse(SIGNAL_UNTRUSTED)
    resolved_budget = resolved.budget if resolved.budget is not None else precheck_budget()
    if not resolved_budget.ok:
        return GuardVerdict.refuse(f"{BUDGET}:{resolved_budget.reason}")
    return GuardVerdict.allow()
