"""SDK-equivalent cost of detached headless Agent-SDK usage.

From 2026-06-15 the Agent SDK bills headless usage against a monthly credit
(Max 20x = $200) at standard API rates, no rollover, no longer covered by the
subscription. This module turns the usage captured on each
:class:`~teatree.core.models.task.TaskAttempt` into the dollar figure that
billing model would charge, so the user can see what teatree's loop would cost.

Two layers. :class:`ModelPrice` / :data:`PRICE_TABLE` hold the per-model list
prices per MTok with the documented cache multipliers (read 0.1x input, write
1.25x input). :func:`attempt_cost_usd` / :class:`CostBreakdown` produce the
per-attempt and cycle-to-date dollar figures: the CLI-reported
``total_cost_usd`` is preferred when present, with the price table as the
fallback so historical rows whose cost was never captured still get an estimate.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from teatree.pricing import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER

_PER_MTOK = 1_000_000
_DECEMBER = 12


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """List price for one model, in dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float

    @property
    def cache_read_per_mtok(self) -> float:
        return self.input_per_mtok * CACHE_READ_MULTIPLIER

    @property
    def cache_write_per_mtok(self) -> float:
        return self.input_per_mtok * CACHE_WRITE_MULTIPLIER

    def cost(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Dollar cost for one usage record at this model's list price."""
        return (
            input_tokens * self.input_per_mtok
            + output_tokens * self.output_per_mtok
            + cache_read_tokens * self.cache_read_per_mtok
            + cache_write_tokens * self.cache_write_per_mtok
        ) / _PER_MTOK


# Tier keys are the short names the model_tiering layer emits (``opus`` /
# ``sonnet`` / ``haiku``) plus the dated full model ids the CLI envelope
# reports under ``modelUsage`` (``claude-opus-4-8`` ...). Lookup normalises a
# model id to its tier (:func:`price_for_model`), so a future dated release of
# the same tier prices correctly without a new entry.
PRICE_TABLE: dict[str, ModelPrice] = {
    "fable": ModelPrice(input_per_mtok=10.0, output_per_mtok=50.0),
    "opus": ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0),
    "sonnet": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0),
    "haiku": ModelPrice(input_per_mtok=1.0, output_per_mtok=5.0),
}

# The reasoning tier is the conservative fallback for an unrecognised model id
# (it never under-estimates the cost of an unknown model).
_DEFAULT_TIER = "opus"

# Capability order, weakest to strongest, for the per-skill model floor merge
# (:func:`teatree.agents.model_tiering.resolve_spawn_model`). Expressed in the
# ABSTRACT tiers (``cheap`` < ``balanced`` < ``frontier``) — the
# :data:`teatree.agents.model_tiering.TIER_MODELS` keys — plus ``fable`` above
# them (the most-honest escalation/kill-switch tier, priced above frontier).
# Distinct from pricing: an unknown full id ranks ABOVE every known tier here
# (treated as most-capable so a below-floor never silently downgrades a spawn),
# whereas :func:`tier_of_model` prices an unknown id at the conservative tier.
_CAPABILITY_ORDER: tuple[str, ...] = ("cheap", "balanced", "frontier", "fable")

# The underlying model FAMILY each abstract tier maps to, so :func:`tier_rank`
# ranks an old short-name (``opus``) or a concrete dated id (``claude-opus-4-8``)
# identically to the abstract tier it belongs to (``frontier``). Keyed by the
# family substring; checked after the abstract tier names so an explicit
# ``frontier`` wins without depending on family. ``fable`` is its own rank.
_FAMILY_TO_TIER: dict[str, str] = {"haiku": "cheap", "sonnet": "balanced", "opus": "frontier"}

# A floor whose capability cannot otherwise be inferred defaults to this abstract
# tier rank — the same conservative-reasoning default the prior order used
# (``opus`` ≡ ``frontier``), so ``None``/inherit never downgrades a phase.
_DEFAULT_CAPABILITY_TIER = "frontier"

# Monthly Agent-SDK credit for a Max 20x subscription.
DEFAULT_MONTHLY_CREDIT_USD = 200.0


def tier_of_model(model: str | None) -> str:
    """Normalise a model id / tier name to a :data:`PRICE_TABLE` tier key.

    Accepts the short tier names (``opus``), the dated CLI ids
    (``claude-opus-4-8``, optionally suffixed ``[1m]``), and ``None``
    (an attempt that inherited the user's default model — priced at the
    reasoning tier). Unknown ids fall back to :data:`_DEFAULT_TIER`.
    """
    if not model:
        return _DEFAULT_TIER
    lowered = model.lower()
    for tier in PRICE_TABLE:
        if tier in lowered:
            return tier
    return _DEFAULT_TIER


def price_for_model(model: str | None) -> ModelPrice:
    """Return the :class:`ModelPrice` for a model id / tier name."""
    return PRICE_TABLE[tier_of_model(model)]


def tier_rank(model: str | None) -> int:
    """Capability rank of a model id / tier name, for the per-skill floor merge.

    Ranks against :data:`_CAPABILITY_ORDER` (``cheap`` 0 < ``balanced`` <
    ``frontier`` < ``fable``). A value is recognised three ways, in order: an
    abstract tier name (``frontier``), a model FAMILY (``opus`` short-name or a
    dated ``claude-opus-4-8`` id, mapped via :data:`_FAMILY_TO_TIER`), and
    ``fable`` (its own top rank). ``None`` and the inherit sentinels (empty
    string) rank as :data:`_DEFAULT_CAPABILITY_TIER` (``frontier``) so a floor
    below the inherited default never silently downgrades a phase. An
    unrecognised full id ranks ABOVE every known tier (most-capable), the
    opposite of :func:`tier_of_model`'s conservative pricing fallback: an unknown
    spawn target is assumed strong so a lower floor never wins over it.
    """
    if not model:
        return _CAPABILITY_ORDER.index(_DEFAULT_CAPABILITY_TIER)
    lowered = model.lower()
    for rank, tier in enumerate(_CAPABILITY_ORDER):
        if tier in lowered:
            return rank
    for family, tier in _FAMILY_TO_TIER.items():
        if family in lowered:
            return _CAPABILITY_ORDER.index(tier)
    return len(_CAPABILITY_ORDER)


@dataclass(frozen=True, slots=True)
class AttemptUsage:
    """The usage fields one :class:`TaskAttempt` carries for costing."""

    model: str | None
    reported_cost_usd: float | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


def price_table_cost_usd(usage: AttemptUsage) -> float:
    """Estimate an attempt's cost from the price table (no CLI cost used)."""
    return price_for_model(usage.model).cost(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


def attempt_cost_usd(usage: AttemptUsage) -> float:
    """SDK-equivalent dollar cost for one attempt.

    Prefers the CLI-reported ``total_cost_usd`` when present (the most
    accurate figure — it already reflects the exact per-iteration rates);
    falls back to the price-table estimate when absent, so historical rows
    whose cost was never captured still contribute an estimate.
    """
    if usage.reported_cost_usd is not None:
        return usage.reported_cost_usd
    return price_table_cost_usd(usage)


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Cycle-to-date SDK-equivalent spend, totalled and split per tier."""

    total_usd: float = 0.0
    per_tier_usd: dict[str, float] = field(default_factory=dict)
    attempts: int = 0

    @classmethod
    def from_usages(cls, usages: Iterable[AttemptUsage]) -> "CostBreakdown":
        per_tier: dict[str, float] = {}
        total = 0.0
        count = 0
        for usage in usages:
            cost = attempt_cost_usd(usage)
            tier = tier_of_model(usage.model)
            per_tier[tier] = per_tier.get(tier, 0.0) + cost
            total += cost
            count += 1
        return cls(total_usd=total, per_tier_usd=per_tier, attempts=count)


def cycle_start(today: date, *, anchor_day: int | None = None) -> date:
    """Start date of the billing cycle containing *today*.

    With *anchor_day* the cycle starts on that day-of-month (the day the SDK
    credit refreshes) — the most recent occurrence on or before *today*,
    clamped into short months. Without it (``None``), the cycle is the
    calendar month.
    """
    if anchor_day is None:
        return today.replace(day=1)
    clamped = _clamp_day(today.year, today.month, anchor_day)
    if today.day >= clamped:
        return today.replace(day=clamped)
    prev_year, prev_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return date(prev_year, prev_month, _clamp_day(prev_year, prev_month, anchor_day))


def _clamp_day(year: int, month: int, day: int) -> int:
    """Clamp *day* into the real length of *month* (anchor 31 → Feb 28/29)."""
    next_month = date(year + 1, 1, 1) if month == _DECEMBER else date(year, month + 1, 1)
    days_in_month = (next_month - timedelta(days=1)).day
    return min(max(day, 1), days_in_month)


def project_month_end_usd(spend_to_date_usd: float, *, cycle_start_date: date, today: date) -> float:
    """Linear end-of-cycle projection from the spend so far.

    Scales the spend-to-date by ``cycle_length / days_elapsed``. The cycle
    length is the gap to the next cycle start (so a custom anchor projects
    over its own ~month-long window, not a calendar month).
    """
    next_start = _next_cycle_start(cycle_start_date)
    cycle_days = max(1, (next_start - cycle_start_date).days)
    days_elapsed = max(1, (today - cycle_start_date).days + 1)
    return spend_to_date_usd * cycle_days / days_elapsed


def _next_cycle_start(cycle_start_date: date) -> date:
    """The cycle start one period after *cycle_start_date* (same anchor day)."""
    anchor = cycle_start_date.day
    if cycle_start_date.month == _DECEMBER:
        year, month = cycle_start_date.year + 1, 1
    else:
        year, month = cycle_start_date.year, cycle_start_date.month + 1
    return date(year, month, _clamp_day(year, month, anchor))


def cycle_start_datetime(today: date, *, anchor_day: int | None = None) -> datetime:
    """Aware ``datetime`` at midnight of the cycle start (for DB filtering)."""
    from datetime import time  # noqa: PLC0415

    from django.utils import timezone  # noqa: PLC0415

    start_date = cycle_start(today, anchor_day=anchor_day)
    midnight = datetime.combine(start_date, time.min)
    return timezone.make_aware(midnight, timezone.get_current_timezone())


@dataclass(frozen=True, slots=True)
class CostReport:
    """A rendered ``t3 cost`` report: cycle-to-date spend vs the SDK credit."""

    breakdown: CostBreakdown
    credit_usd: float
    cycle_start_date: date
    today: date
    projected_month_end_usd: float

    @classmethod
    def build(
        cls,
        breakdown: CostBreakdown,
        *,
        credit_usd: float,
        cycle_start_date: date,
        today: date,
    ) -> "CostReport":
        return cls(
            breakdown=breakdown,
            credit_usd=credit_usd,
            cycle_start_date=cycle_start_date,
            today=today,
            projected_month_end_usd=project_month_end_usd(
                breakdown.total_usd,
                cycle_start_date=cycle_start_date,
                today=today,
            ),
        )

    def chip(self) -> str:
        """Compact statusline chip, e.g. ``SDK mtd ≈$48/$200``.

        ``mtd`` (month-to-date) names the accumulation window explicitly so the
        figure is unambiguous next to the weekly rate-limit segment: it is the
        spend since :attr:`cycle_start_date` (the monthly Agent-SDK billing
        cycle, anchored to ``billing_cycle_anchor_day`` or the calendar month),
        not a 5-hour or 7-day window. The ``/$<credit>`` is the monthly credit.

        Stays tiny at any spend: whole dollars, no decimals, no thousands
        separators that would balloon the width.
        """
        return f"SDK mtd ≈${round(self.breakdown.total_usd)}/${round(self.credit_usd)}"

    def render_lines(self) -> list[str]:
        """Human-readable multi-line report body."""
        spent = self.breakdown.total_usd
        pct = (spent / self.credit_usd * 100) if self.credit_usd else 0.0
        lines = [
            f"SDK-equivalent spend (cycle from {self.cycle_start_date.isoformat()})",
            f"  cycle-to-date: ${spent:,.2f} / ${self.credit_usd:,.0f} credit ({pct:.0f}%)",
            f"  projected end-of-cycle: ${self.projected_month_end_usd:,.2f}",
            f"  attempts: {self.breakdown.attempts}",
        ]
        if self.breakdown.per_tier_usd:
            lines.append("  per model:")
            for tier, amount in sorted(self.breakdown.per_tier_usd.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {tier}: ${amount:,.2f}")
        return lines


def register_cost_factories() -> None:
    """Register the cost dataclasses the ``TaskAttempt`` queryset reaches DOWN to (#2385).

    ``cost.py`` depends ON the models indirectly and must NOT move into
    ``modelkit`` (it would break PR-1's ``depends_on == []`` leaf test), so the
    models can't import it without an intra-core up-edge. The registry inverts
    the edge: the model fetches ``AttemptUsage`` / ``CostBreakdown`` by name at
    call time.
    """
    from teatree.core.modelkit.gate_registry import register  # noqa: PLC0415

    register("cost", "AttemptUsage", AttemptUsage)
    register("cost", "CostBreakdown", CostBreakdown)
