"""Per-phase usage aggregation over REAL dispatch attempts.

``teatree_taskattempt`` mixes two populations. A real agent dispatch records
``model`` / token counts / ``cost_usd``; a park-audit row (``limit_parked:`` /
``stuck_loop:``) records none of them and outnumbers the real rows ~280:1, so a
naive aggregate over the table measures the park loop, not the fleet.
:class:`AttemptRecord` is therefore the shape of a REAL dispatch only — the
caller filters, and :func:`aggregate_by_phase` / :func:`aggregate_by_phase_model`
never see a park row.

Median and p95 are both reported: a model swap shows up in the tail before it
shows up in the middle, and the tail is where a runaway lives.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass

_MEDIAN = 0.5
_P95 = 0.95


def percentile(values: Iterable[float], fraction: float) -> float:
    """Nearest-rank percentile — an observation, never an interpolated invention.

    Interpolation would report a figure no attempt actually recorded, which on a
    3-attempt phase group is most of the signal. Nearest-rank keeps every
    reported number a real measurement.
    """
    ordered = sorted(values)
    if not ordered:
        msg = "percentile of an empty sample is undefined"
        raise ValueError(msg)
    rank = math.ceil(fraction * len(ordered))
    return float(ordered[max(rank, 1) - 1])


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One real agent dispatch, as the cost baseline reads it."""

    phase: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    num_turns: int
    cost_usd: float

    @property
    def billed_input_tokens(self) -> int:
        """Prompt plus both cache dimensions — everything charged on the input side."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True, slots=True)
class UsageStats:
    """The distribution of one group of attempts, in the four billed dimensions."""

    attempts: int
    median_output_tokens: float
    p95_output_tokens: float
    median_billed_input_tokens: float
    p95_billed_input_tokens: float
    median_num_turns: float
    p95_num_turns: float
    median_cost_usd: float
    p95_cost_usd: float
    total_cost_usd: float


def _stats(records: list[AttemptRecord]) -> UsageStats:
    outputs = [float(r.output_tokens) for r in records]
    billed = [float(r.billed_input_tokens) for r in records]
    turns = [float(r.num_turns) for r in records]
    costs = [r.cost_usd for r in records]
    return UsageStats(
        attempts=len(records),
        median_output_tokens=percentile(outputs, _MEDIAN),
        p95_output_tokens=percentile(outputs, _P95),
        median_billed_input_tokens=percentile(billed, _MEDIAN),
        p95_billed_input_tokens=percentile(billed, _P95),
        median_num_turns=percentile(turns, _MEDIAN),
        p95_num_turns=percentile(turns, _P95),
        median_cost_usd=percentile(costs, _MEDIAN),
        p95_cost_usd=percentile(costs, _P95),
        total_cost_usd=sum(costs),
    )


def aggregate_by_phase(records: Iterable[AttemptRecord]) -> dict[str, UsageStats]:
    """Group by phase alone — the comparison axis a model swap does not move.

    A cutover changes which model serves a phase, so a per-``(phase, model)``
    comparison has no counterpart on the other side of it. The phase does have
    one, which is what makes a before/after answerable at all.
    """
    groups: dict[str, list[AttemptRecord]] = {}
    for record in records:
        groups.setdefault(record.phase, []).append(record)
    return {phase: _stats(rows) for phase, rows in sorted(groups.items())}


def aggregate_by_phase_model(records: Iterable[AttemptRecord]) -> dict[tuple[str, str], UsageStats]:
    """Group by ``(phase, model)`` — the finer evidence behind the per-phase roll-up."""
    groups: dict[tuple[str, str], list[AttemptRecord]] = {}
    for record in records:
        groups.setdefault((record.phase, record.model), []).append(record)
    return {key: _stats(rows) for key, rows in sorted(groups.items())}
