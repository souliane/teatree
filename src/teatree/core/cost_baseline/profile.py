"""Where the fleet's money goes: read share, resume-context cost and the addressable upper bounds.

``ProfiledAttempt`` is one real dispatch with every figure the profile reads;
``CostProfile.build`` turns a sample of them into a report. The report is DB-free:
loading rows, and failing loud on an unreadable store, belong to the caller.

The cache read share is ``cache_read / (input + cache_read + cache_write)`` over
known-usage attempts only, weighted by tokens; its denominator is `UsageGroup.cacheable_input_tokens`.

Every addressable figure is an UPPER BOUND, and the three are not additive: one attempt can be
flagged by several items. The union therefore counts each flagged attempt once, at its
largest single-item amount, and the overlap matrix says where the items collide.
"""

import itertools
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from teatree.core.cost import FAMILY_TO_TIER, UNATTRIBUTED_LANE, ModelPrice, UsageGroup, price_for_model, tier_of_model
from teatree.core.cost_baseline.aggregate import AttemptRecord, UsageStats, percentile, usage_stats
from teatree.core.models.task_attempt import RESUME_CONTEXT_CAP_TOKENS
from teatree.hooks.cache_wait_detect import CACHE_WAIT_LIMIT_SECONDS

if TYPE_CHECKING:
    from teatree.core.models.task_attempt import TaskAttempt

LONG_TOOL_CALL_MS = CACHE_WAIT_LIMIT_SECONDS * 1000
CONTEXT_THRESHOLD_TOKENS = (120_000, 200_000, 300_000)

RESUME_CONTEXT_ITEM = "item2_resume_context"
EFFORT_OUTPUT_ITEM = "item3_effort_output"
LONG_WAITS_ITEM = "item4_long_waits"

_PER_MTOK = 1_000_000
_BALANCED_TIER = "balanced"
_MEDIAN = 0.5
_P90 = 0.9
_P95 = 0.95
_MIN_SHOWN_SHARE = 0.001

ContextTrail = Mapping[int, Sequence[tuple[int, int | None]]]


class CostProfileError(Exception):
    """The sample cannot support a profile; the message names the cause."""


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _long_tool_calls(result: Mapping[str, object]) -> int | None:
    calls = result.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return None
    return sum(1 for call in calls if isinstance(call, dict) and (call.get("duration_ms") or 0) >= LONG_TOOL_CALL_MS)


def _carried_context(attempt: "TaskAttempt", trail: ContextTrail) -> int | None:
    earlier = [context for attempt_id, context in trail.get(attempt.task_id, ()) if attempt_id < attempt.pk]
    if earlier:
        return earlier[-1]
    task = attempt.task
    if task.session_continuation != task.SessionContinuation.PARENT or task.parent_task_id is None:
        return None
    before = [context for attempt_id, context in trail.get(task.parent_task_id, ()) if attempt_id < attempt.pk]
    return before[-1] if before else None


@dataclass(frozen=True, slots=True)
class ProfiledAttempt:
    attempt_id: int
    phase: str
    tier: str
    lane: str
    reasoning_effort: str
    failed: bool
    iteration: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    num_turns: int
    context_tokens: int | None
    context_window_tokens: int | None
    carried_context_tokens: int | None
    long_tool_calls: int | None
    price: ModelPrice

    @classmethod
    def of(cls, attempt: "TaskAttempt", *, trail: ContextTrail, price: ModelPrice) -> Self:
        usage = UsageGroup(
            model=attempt.model or None,
            lane=attempt.lane,
            phase=attempt.task.phase,
            estimated=attempt.cost_is_estimated,
            reported_cost_usd=attempt.cost_usd,
            input_tokens=attempt.input_tokens or 0,
            output_tokens=attempt.output_tokens or 0,
            cache_read_tokens=attempt.cache_read_tokens or 0,
            cache_write_tokens=attempt.cache_write_tokens or 0,
            attempts=1,
        )
        return cls(
            attempt_id=attempt.pk,
            phase=attempt.task.phase,
            tier=tier_of_model(attempt.model or None),
            lane=attempt.lane or UNATTRIBUTED_LANE,
            reasoning_effort=attempt.reasoning_effort,
            failed=attempt.outcome in attempt.FAILED_OUTCOMES,
            iteration=attempt.iteration,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=usage.cost_usd(),
            num_turns=attempt.num_turns or 0,
            context_tokens=attempt.context_tokens,
            context_window_tokens=attempt.context_window_tokens,
            carried_context_tokens=_carried_context(attempt, trail),
            long_tool_calls=_long_tool_calls(attempt.result),
            price=price,
        )

    @classmethod
    def of_all(cls, attempts: Iterable["TaskAttempt"], *, trail: ContextTrail) -> list[Self]:
        prices: dict[str, ModelPrice] = {}
        return [
            cls.of(attempt, trail=trail, price=prices.setdefault(attempt.model, price_for_model(attempt.model or None)))
            for attempt in attempts
        ]


@dataclass(frozen=True, slots=True)
class TokenTotals:
    pooled: UsageGroup

    @classmethod
    def of(cls, attempts: Iterable[ProfiledAttempt]) -> Self:
        rows = list(attempts)
        return cls(
            UsageGroup(
                model=None,
                lane="",
                phase="",
                estimated=False,
                reported_cost_usd=None,
                input_tokens=sum(a.input_tokens for a in rows),
                output_tokens=sum(a.output_tokens for a in rows),
                cache_read_tokens=sum(a.cache_read_tokens for a in rows),
                cache_write_tokens=sum(a.cache_write_tokens for a in rows),
                attempts=len(rows),
            )
        )

    @property
    def read_share(self) -> float | None:
        return _ratio(self.pooled.cache_read_tokens, self.pooled.cacheable_input_tokens)


def _read_share_by(
    attempts: Sequence[ProfiledAttempt], key: Callable[[ProfiledAttempt], str]
) -> dict[str, float | None]:
    groups: dict[str, list[ProfiledAttempt]] = defaultdict(list)
    for attempt in attempts:
        groups[key(attempt)].append(attempt)
    return {name: TokenTotals.of(rows).read_share for name, rows in sorted(groups.items())}


@dataclass(frozen=True, slots=True)
class ReadShare:
    overall: float | None
    per_lane: dict[str, float | None]
    per_phase: dict[str, float | None]
    per_tier: dict[str, float | None]

    @classmethod
    def of(cls, attempts: Sequence[ProfiledAttempt]) -> Self:
        return cls(
            overall=TokenTotals.of(attempts).read_share,
            per_lane=_read_share_by(attempts, lambda a: a.lane),
            per_phase=_read_share_by(attempts, lambda a: a.phase),
            per_tier=_read_share_by(attempts, lambda a: a.tier),
        )


@dataclass(frozen=True, slots=True)
class ThresholdShare:
    threshold_tokens: int
    attempts_above: int
    attempt_share: float
    cost_share: float | None


@dataclass(frozen=True, slots=True)
class ContextDistribution:
    measured_attempts: int
    unmeasured_attempts: int
    p50_tokens: float
    p90_tokens: float
    p95_tokens: float
    max_tokens: float
    above: tuple[ThresholdShare, ...]

    @classmethod
    def of(cls, attempts: Sequence[ProfiledAttempt]) -> Self:
        measured = [(a.context_tokens, a.cost_usd) for a in attempts if a.context_tokens is not None]
        if not measured:
            msg = "every context_tokens is NULL in the sample, so context size and resume cost are unmeasured"
            raise CostProfileError(msg)
        contexts = [float(context) for context, _ in measured]
        total_cost = sum(cost for _, cost in measured)
        above = []
        for threshold in CONTEXT_THRESHOLD_TOKENS:
            over = [cost for context, cost in measured if context > threshold]
            above.append(
                ThresholdShare(
                    threshold_tokens=threshold,
                    attempts_above=len(over),
                    attempt_share=len(over) / len(measured),
                    cost_share=_ratio(sum(over), total_cost),
                )
            )
        return cls(
            measured_attempts=len(measured),
            unmeasured_attempts=len(attempts) - len(measured),
            p50_tokens=percentile(contexts, _MEDIAN),
            p90_tokens=percentile(contexts, _P90),
            p95_tokens=percentile(contexts, _P95),
            max_tokens=max(contexts),
            above=tuple(above),
        )


@dataclass(frozen=True, slots=True)
class EffortCell:
    phase: str
    tier: str
    reasoning_effort: str
    stats: UsageStats
    failure_rate: float
    mean_iteration: float


def _effort_cells(attempts: Sequence[ProfiledAttempt]) -> tuple[EffortCell, ...]:
    groups: dict[tuple[str, str, str], list[ProfiledAttempt]] = defaultdict(list)
    for attempt in attempts:
        groups[attempt.phase, attempt.tier, attempt.reasoning_effort].append(attempt)
    return tuple(
        EffortCell(
            phase=phase,
            tier=tier,
            reasoning_effort=effort,
            stats=usage_stats(
                [
                    AttemptRecord(
                        phase=phase,
                        model=tier,
                        input_tokens=a.input_tokens,
                        output_tokens=a.output_tokens,
                        cache_read_tokens=a.cache_read_tokens,
                        cache_write_tokens=a.cache_write_tokens,
                        num_turns=a.num_turns,
                        cost_usd=a.cost_usd,
                    )
                    for a in rows
                ]
            ),
            failure_rate=sum(a.failed for a in rows) / len(rows),
            mean_iteration=sum(a.iteration for a in rows) / len(rows),
        )
        for (phase, tier, effort), rows in sorted(groups.items())
    )


@dataclass(frozen=True, slots=True)
class _ItemSpec:
    name: str
    amount: Callable[[ProfiledAttempt], float]
    measured: Callable[[ProfiledAttempt], bool] | None = None

    def is_measured(self, attempt: ProfiledAttempt) -> bool:
        return self.measured is None or self.measured(attempt)


def _resume_context_usd(attempt: ProfiledAttempt) -> float:
    carried = attempt.carried_context_tokens or 0
    return attempt.cost_usd if carried > RESUME_CONTEXT_CAP_TOKENS else 0.0


def _resume_context_measured(attempt: ProfiledAttempt) -> bool:
    return attempt.carried_context_tokens is not None


def _effort_output_usd(attempt: ProfiledAttempt) -> float:
    if FAMILY_TO_TIER.get(attempt.tier) != _BALANCED_TIER:
        return 0.0
    return attempt.output_tokens * attempt.price.output_per_mtok / _PER_MTOK


def _long_waits_measured(attempt: ProfiledAttempt) -> bool:
    return attempt.long_tool_calls is not None and attempt.context_tokens is not None


def _long_waits_usd(attempt: ProfiledAttempt) -> float:
    premium_per_mtok = attempt.price.cache_write_per_mtok - attempt.price.cache_read_per_mtok
    return (attempt.long_tool_calls or 0) * (attempt.context_tokens or 0) * premium_per_mtok / _PER_MTOK


_ITEMS = (
    _ItemSpec(RESUME_CONTEXT_ITEM, _resume_context_usd, _resume_context_measured),
    _ItemSpec(EFFORT_OUTPUT_ITEM, _effort_output_usd),
    _ItemSpec(LONG_WAITS_ITEM, _long_waits_usd, _long_waits_measured),
)


@dataclass(frozen=True, slots=True)
class Addressable:
    item: str
    usd_upper_bound: float | None
    share_of_total: float | None
    flagged_attempts: int
    measured_attempts: int
    unmeasured_attempts: int

    @classmethod
    def of(cls, spec: _ItemSpec, attempts: Sequence[ProfiledAttempt], *, total_cost_usd: float) -> Self:
        amounts = [spec.amount(a) for a in attempts if spec.is_measured(a)]
        usd = sum(amounts) if amounts else None
        return cls(
            item=spec.name,
            usd_upper_bound=usd,
            share_of_total=None if usd is None else _ratio(usd, total_cost_usd),
            flagged_attempts=sum(1 for amount in amounts if amount > 0),
            measured_attempts=len(amounts),
            unmeasured_attempts=len(attempts) - len(amounts),
        )


@dataclass(frozen=True, slots=True)
class OverlapCell:
    first: str
    second: str
    attempts: int
    usd: float

    @classmethod
    def between(cls, first: str, first_amounts: Sequence[float], second: str, second_amounts: Sequence[float]) -> Self:
        pairs = list(zip(first_amounts, second_amounts, strict=True))
        return cls(
            first=first,
            second=second,
            attempts=sum(1 for x, y in pairs if x > 0 and y > 0),
            usd=sum(itertools.starmap(min, pairs)),
        )


@dataclass(frozen=True, slots=True)
class UnionBound:
    usd: float | None
    share_of_total: float | None
    flagged_attempts: int
    flagged_attempt_cost_usd: float
    sum_of_bounds_usd: float
    largest_bound_usd: float
    overlaps: tuple[OverlapCell, ...]

    @classmethod
    def of(cls, specs: Sequence[_ItemSpec], attempts: Sequence[ProfiledAttempt], *, total_cost_usd: float) -> Self:
        columns = {
            spec.name: [spec.amount(a) if spec.is_measured(a) else 0.0 for a in attempts]
            for spec in specs
            if any(spec.is_measured(a) for a in attempts)
        }
        rows = list(zip(*columns.values(), strict=True))
        union = sum(max(row) for row in rows) if rows else None
        bounds = [sum(column) for column in columns.values()]
        return cls(
            usd=union,
            share_of_total=None if union is None else _ratio(union, total_cost_usd),
            flagged_attempts=sum(1 for row in rows if max(row) > 0),
            flagged_attempt_cost_usd=sum(a.cost_usd for a, row in zip(attempts, rows, strict=True) if max(row) > 0),
            sum_of_bounds_usd=sum(bounds),
            largest_bound_usd=max(bounds, default=0.0),
            overlaps=tuple(
                OverlapCell.between(first, columns[first], second, columns[second])
                for first, second in itertools.combinations(columns, 2)
            ),
        )


@dataclass(frozen=True, slots=True)
class CostProfile:
    attempts: int
    total_cost_usd: float
    read_share: ReadShare
    context: ContextDistribution
    effort_cells: tuple[EffortCell, ...]
    addressable: tuple[Addressable, ...]
    union: UnionBound

    @classmethod
    def build(cls, rows: Sequence[ProfiledAttempt]) -> Self:
        if not rows:
            msg = "no real dispatches in the sample (non-park, output tokens recorded, usage known)"
            raise CostProfileError(msg)
        total = sum(a.cost_usd for a in rows)
        return cls(
            attempts=len(rows),
            total_cost_usd=total,
            read_share=ReadShare.of(rows),
            context=ContextDistribution.of(rows),
            effort_cells=_effort_cells(rows),
            addressable=tuple(Addressable.of(spec, rows, total_cost_usd=total) for spec in _ITEMS),
            union=UnionBound.of(_ITEMS, rows, total_cost_usd=total),
        )


def _pct(share: float | None) -> str:
    if share is None:
        return "unmeasured"
    return "<0.1%" if 0 < share < _MIN_SHOWN_SHARE else f"{share:.1%}"


def _usd(amount: float | None) -> str:
    return "unmeasured" if amount is None else f"${amount:,.2f}"


def _share_lines(label: str, shares: Mapping[str, float | None]) -> list[str]:
    return [f"  {label} {name}: {_pct(share)}" for name, share in shares.items()]


def _item_line(entry: Addressable) -> str:
    if entry.usd_upper_bound is None:
        return f"  {entry.item}: unmeasured - {entry.unmeasured_attempts} attempts lack the inputs it needs"
    return (
        f"  {entry.item}: {_usd(entry.usd_upper_bound)} upper bound ({_pct(entry.share_of_total)} of total)"
        f" - flagged {entry.flagged_attempts} of {entry.measured_attempts} measured,"
        f" {entry.unmeasured_attempts} unmeasured"
    )


def _render_context(context: ContextDistribution) -> list[str]:
    lines = [
        (
            f"context_tokens ({context.measured_attempts} measured, {context.unmeasured_attempts} NULL):"
            f" p50 {context.p50_tokens:,.0f}, p90 {context.p90_tokens:,.0f},"
            f" p95 {context.p95_tokens:,.0f}, max {context.max_tokens:,.0f}"
        )
    ]
    lines.extend(
        f"  above {entry.threshold_tokens:,}: {_pct(entry.attempt_share)} of attempts, {_pct(entry.cost_share)} of cost"
        for entry in context.above
    )
    return lines


def _render_union(union: UnionBound) -> list[str]:
    if union.usd is None:
        return ["union: unmeasured - no item has a measurable attempt"]
    lines = [
        (
            f"union (each flagged attempt counted once): {_usd(union.usd)} upper bound"
            f" ({_pct(union.share_of_total)} of total) <= sum of item bounds {_usd(union.sum_of_bounds_usd)},"
            f" >= largest {_usd(union.largest_bound_usd)}; {union.flagged_attempts} attempts flagged,"
            f" costing {_usd(union.flagged_attempt_cost_usd)} in all"
        )
    ]
    lines.extend(
        f"  overlap {cell.first} x {cell.second}: {cell.attempts} attempts, {_usd(cell.usd)}" for cell in union.overlaps
    )
    return lines


def render_lines(profile: CostProfile) -> list[str]:
    lines = [
        (
            f"cost profile: {profile.attempts} real dispatches, {_usd(profile.total_cost_usd)} total"
            " (park and unknown-usage rows excluded)"
        ),
        f"read share: overall {_pct(profile.read_share.overall)}",
        *_share_lines("lane", profile.read_share.per_lane),
        *_share_lines("phase", profile.read_share.per_phase),
        *_share_lines("tier", profile.read_share.per_tier),
        *_render_context(profile.context),
        "effort baseline (phase / tier / effort):",
    ]
    lines.extend(
        f"  {cell.phase} / {cell.tier} / {cell.reasoning_effort or 'inherit'}: {cell.stats.attempts} attempts,"
        f" median {_usd(cell.stats.median_cost_usd)}, p95 {_usd(cell.stats.p95_cost_usd)},"
        f" failure {_pct(cell.failure_rate)}, mean iteration {cell.mean_iteration:.2f}"
        for cell in profile.effort_cells
    )
    lines.append("addressable spend (upper bounds, not additive):")
    lines.extend(_item_line(entry) for entry in profile.addressable)
    lines.extend(_render_union(profile.union))
    return lines


__all__ = [
    "Addressable",
    "ContextDistribution",
    "CostProfile",
    "CostProfileError",
    "EffortCell",
    "OverlapCell",
    "ProfiledAttempt",
    "ReadShare",
    "ThresholdShare",
    "TokenTotals",
    "UnionBound",
    "render_lines",
]
