"""Derived-on-read factory quality/velocity signals (SIG-PR-1).

Five quantitative signals computed over the ledgers the FSM and merge/review
loops already record — ``MergeAudit``/``MergeClear`` (§17.4), ``ReviewVerdict``
+ ``Finding`` severities, ``RedMrFixAttempt``, ``TaskAttempt``, and
``Ticket``/``TicketTransition``/``RedCardSignal``. Mirrors
:mod:`teatree.core.standup`'s read-only-aggregator doctrine: no new models, no
migrations, no LLM calls, no network — every query underneath is a select, so
this runs for free in any session or future outer-loop tick.

This module owns the public report model and composition; the ledger queries
live in :mod:`teatree.core.factory_signal_queries`. Each signal fails loud,
never fake-green: it is a **provider-shaped** function returning
:class:`SignalReading` (value, sample_size, window_days, status ∈
{ok, insufficient_data, instrumentation_gap}) so the PR-2 recipe registry can
register them verbatim. A sample below :data:`~teatree.core.factory_signal_queries.MIN_SAMPLE`
reports ``insufficient_data``; a provably-silent upstream recorder reports
``instrumentation_gap`` — never a fabricated 100%. :func:`compute_factory_signals`
composes the five readings against the immediately-preceding window as a rolling
baseline into a :class:`FactorySignalsReport` whose per-signal rows carry the
red-floor verdict (a rubber-stamp review window, a stalled merge loop) the outer
loop keys on.
"""

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.utils import timezone

from teatree.core.factory_signal_queries import (
    Computation,
    SignalReading,
    SignalStatus,
    Window,
    baseline_window,
    compute_s1,
    compute_s2,
    compute_s3,
    compute_s4,
    compute_s5,
    current_window,
)

# Re-exported so the signal surface is importable from one module.
__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "Direction",
    "FactorySignalsReport",
    "SignalReading",
    "SignalRow",
    "SignalStatus",
    "SignalVerdict",
    "compute_factory_signals",
    "defect_escape_rate",
    "first_try_green_rate",
    "merge_latency",
    "repair_iteration_burn",
    "review_catch_rate",
]

DEFAULT_WINDOW_DAYS = 28


class SignalVerdict(enum.StrEnum):
    """A per-signal-row (and top-level report) verdict.

    Richer than :class:`SignalStatus`: it folds the reading's status together
    with the red-floor trip and the rolling-baseline delta into the value the
    outer loop reads. ``insufficient_data`` / ``instrumentation_gap`` mirror the
    reading status; ``red`` is a tripped hard floor or a silent recorder;
    ``regressing`` is a drift worse than the preceding window; ``ok`` is healthy.
    """

    OK = "ok"
    REGRESSING = "regressing"
    RED = "red"
    INSUFFICIENT_DATA = "insufficient_data"
    INSTRUMENTATION_GAP = "instrumentation_gap"


class Direction(enum.StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


@dataclass(frozen=True, slots=True)
class SignalRow:
    """One signal in the report: its reading, rolling baseline, and verdict."""

    provider_id: str
    kind: str
    reading: SignalReading
    direction: Direction
    red_when: float | None
    baseline_value: float | None
    delta: float | None
    tripped: bool
    verdict: SignalVerdict
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "value": self.reading.value,
            "sample_size": self.reading.sample_size,
            "window_days": self.reading.window_days,
            "status": self.reading.status.value,
            "direction": self.direction.value,
            "red_when": self.red_when,
            "baseline_value": self.baseline_value,
            "delta": self.delta,
            "tripped": self.tripped,
            "verdict": self.verdict.value,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class FactorySignalsReport:
    """The composed read-only report over the trailing window vs its baseline."""

    window_days: int
    generated_at: datetime
    signals: list[SignalRow]
    verdict: SignalVerdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "generated_at": self.generated_at.isoformat(),
            "verdict": self.verdict.value,
            "signals": [row.to_dict() for row in self.signals],
        }

    def to_markdown(self) -> str:
        header = f"## Factory signals (window {self.window_days}d) — verdict: {self.verdict.value}"
        lines = [header, "", "| signal | value | sample | status | verdict | baseline |", "|---|---|---|---|---|---|"]
        for row in self.signals:
            baseline = "—" if row.baseline_value is None else f"{row.baseline_value:.3f}"
            lines.append(
                f"| {row.provider_id} | {row.reading.value:.3f} | {row.reading.sample_size} "
                f"| {row.reading.status.value} | {row.verdict.value} | {baseline} |",
            )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class SignalSpec:
    """A registered signal: its id, direction, red floor, and compute path.

    The star-shaped registry PR-2's recipe imports — the recipe names
    ``provider_id`` and an unknown id is a load-time error; composition is by
    registration, never cross-import. ``regress_band`` is the drift beyond the
    baseline that counts as regressing: an additive band by default (worse by
    more than ``regress_band`` in the signal's bad direction), or a multiplier
    when ``regress_multiplicative`` (S4: latency worse than ``regress_band``
    times baseline).
    """

    provider_id: str
    kind: str
    direction: Direction
    compute: Callable[[Window, str, datetime], Computation]
    red_when: float | None
    regress_band: float
    regress_multiplicative: bool = False

    def regressed(self, value: float, baseline: float) -> bool:
        if self.regress_multiplicative:
            return baseline > 0 and value > self.regress_band * baseline
        if self.direction == Direction.HIGHER_IS_BETTER:
            return value < baseline - self.regress_band
        return value > baseline + self.regress_band


SIGNALS: tuple[SignalSpec, ...] = (
    SignalSpec("first_try_green", "quant", Direction.HIGHER_IS_BETTER, compute_s1, 0.5, 0.1),
    SignalSpec("defect_escape", "quant", Direction.LOWER_IS_BETTER, compute_s2, None, 0.1),
    SignalSpec("review_catch", "quant", Direction.HIGHER_IS_BETTER, compute_s3, 0.0, 0.1),
    SignalSpec("merge_latency", "quant", Direction.LOWER_IS_BETTER, compute_s4, None, 2.0, regress_multiplicative=True),
    SignalSpec("repair_burn", "quant", Direction.LOWER_IS_BETTER, compute_s5, None, 0.5),
)


def _provider(
    compute: Callable[[Window, str, datetime], Computation],
    window_days: int,
    overlay: str,
    now: datetime | None,
) -> SignalReading:
    resolved = now or timezone.now()
    return compute(current_window(resolved, window_days), overlay, resolved).reading


def first_try_green_rate(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    overlay: str = "",
    now: datetime | None = None,
) -> SignalReading:
    return _provider(compute_s1, window_days, overlay, now)


def defect_escape_rate(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    overlay: str = "",
    now: datetime | None = None,
) -> SignalReading:
    return _provider(compute_s2, window_days, overlay, now)


def review_catch_rate(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    overlay: str = "",
    now: datetime | None = None,
) -> SignalReading:
    return _provider(compute_s3, window_days, overlay, now)


def merge_latency(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    overlay: str = "",
    now: datetime | None = None,
) -> SignalReading:
    return _provider(compute_s4, window_days, overlay, now)


def repair_iteration_burn(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    overlay: str = "",
    now: datetime | None = None,
) -> SignalReading:
    return _provider(compute_s5, window_days, overlay, now)


def _floor_tripped(value: float, spec: SignalSpec) -> bool:
    if spec.red_when is None:
        return False
    if spec.direction == Direction.HIGHER_IS_BETTER:
        return value <= spec.red_when
    return value >= spec.red_when


def _row_verdict(spec: SignalSpec, comp: Computation, baseline_value: float | None) -> tuple[SignalVerdict, bool]:
    """Fold reading status + hard-red trip + rolling-baseline delta into a verdict.

    Order matters: a silent recorder and a companion hard-red (S4 stale CLEAR)
    both win over an insufficient sample, so a stalled merge loop is never
    masked by a thin window.
    """
    reading = comp.reading
    if reading.status == SignalStatus.INSTRUMENTATION_GAP:
        return SignalVerdict.INSTRUMENTATION_GAP, False
    if comp.hard_red:
        return SignalVerdict.RED, True
    if reading.status == SignalStatus.OK and _floor_tripped(reading.value, spec):
        return SignalVerdict.RED, True
    if reading.status == SignalStatus.INSUFFICIENT_DATA:
        return SignalVerdict.INSUFFICIENT_DATA, False
    if baseline_value is not None and spec.regressed(reading.value, baseline_value):
        return SignalVerdict.REGRESSING, False
    return SignalVerdict.OK, False


def _build_row(spec: SignalSpec, comp: Computation, baseline_value: float | None) -> SignalRow:
    verdict, tripped = _row_verdict(spec, comp, baseline_value)
    delta = (
        comp.reading.value - baseline_value
        if baseline_value is not None and comp.reading.status == SignalStatus.OK
        else None
    )
    return SignalRow(
        provider_id=spec.provider_id,
        kind=spec.kind,
        reading=comp.reading,
        direction=spec.direction,
        red_when=spec.red_when,
        baseline_value=baseline_value,
        delta=delta,
        tripped=tripped,
        verdict=verdict,
        evidence=comp.evidence,
    )


def _aggregate_verdict(rows: list[SignalRow]) -> SignalVerdict:
    """RED if any signal is RED or its recorder is silent, else REGRESSING, else OK.

    Starving a signal can only lower the verdict — an ``instrumentation_gap`` is
    RED, never a free pass.
    """
    if any(row.verdict in {SignalVerdict.RED, SignalVerdict.INSTRUMENTATION_GAP} for row in rows):
        return SignalVerdict.RED
    if any(row.verdict == SignalVerdict.REGRESSING for row in rows):
        return SignalVerdict.REGRESSING
    return SignalVerdict.OK


def compute_factory_signals(
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    overlay: str = "",
    now: datetime | None = None,
) -> FactorySignalsReport:
    """Compose the five signals over the trailing window vs its preceding baseline.

    Pure read path: aggregates the merge/review/CI/repair ledgers, never
    mutating a row. The baseline is the immediately-preceding window of the same
    width; a baseline whose own reading is not ``ok`` contributes no delta.
    """
    resolved = now or timezone.now()
    current = current_window(resolved, window_days)
    baseline = baseline_window(resolved, window_days)
    rows: list[SignalRow] = []
    for spec in SIGNALS:
        comp = spec.compute(current, overlay, resolved)
        base = spec.compute(baseline, overlay, resolved)
        baseline_value = base.reading.value if base.reading.status == SignalStatus.OK else None
        rows.append(_build_row(spec, comp, baseline_value))
    return FactorySignalsReport(
        window_days=window_days,
        generated_at=resolved,
        signals=rows,
        verdict=_aggregate_verdict(rows),
    )
