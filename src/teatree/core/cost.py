"""SDK-equivalent cost of detached headless Agent-SDK usage.

From 2026-06-15 the Agent SDK bills headless usage against a monthly credit
(Max 20x = $200) at standard API rates, no rollover, no longer covered by the
subscription. This module turns the usage captured on each
:class:`~teatree.core.models.task_attempt.TaskAttempt` into the dollar figure that
billing model would charge, so the user can see what teatree's loop would cost.

Two layers. :class:`ModelPrice` / :data:`PRICE_TABLE` hold the per-model list
prices per MTok with the documented cache multipliers (read 0.1x input, write
1.25x input). :func:`attempt_cost_usd` / :class:`CostBreakdown` produce the
per-attempt and cycle-to-date dollar figures: the CLI-reported
``total_cost_usd`` is preferred when present, with the price table as the
fallback so historical rows whose cost was never captured still get an estimate.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from teatree.config import cold_reader
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


# The abstract-tier / model-id token whose usage carries NO built-in Claude
# price. A present but unrecognised model id (a swapped GPT/open-source model
# with no ``cost_model_prices`` override) resolves here instead of being SILENTLY
# folded into the ``opus`` tier: it is still billed CONSERVATIVELY at the opus
# rate (:data:`PRICE_TABLE`), but it buckets under its own key so ``t3 cost``
# flags the figure as an un-vetted estimate rather than confidently-wrong Claude
# spend (§3a #2, §7 #4).
UNPRICED_TIER = "unpriced"

# Tier keys are the short names the model_tiering layer emits (``opus`` /
# ``sonnet`` / ``haiku``) plus the dated full model ids the CLI envelope
# reports under ``modelUsage`` (``claude-opus-4-8`` ...). Lookup normalises a
# model id to its tier (:func:`price_for_model`), so a future dated release of
# the same tier prices correctly without a new entry. :data:`UNPRICED_TIER` is
# priced at the conservative opus rate but kept a distinct bucket (above).
PRICE_TABLE: dict[str, ModelPrice] = {
    "opus": ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0),
    "sonnet": ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0),
    "haiku": ModelPrice(input_per_mtok=1.0, output_per_mtok=5.0),
    UNPRICED_TIER: ModelPrice(input_per_mtok=5.0, output_per_mtok=25.0),
}

# The Claude family short-names :func:`tier_of_model` matches (by substring) in a
# model id before falling back to :data:`UNPRICED_TIER`. Explicit so the fallback
# key (``unpriced``) is never itself a match target.
_BUILTIN_TIERS: tuple[str, ...] = ("opus", "sonnet", "haiku")

# The conservative reasoning tier a ``None`` model (an attempt that inherited the
# user's own default model, whose id was never captured) is priced at — distinct
# from :data:`UNPRICED_TIER`, which is for a PRESENT but unrecognised id.
_DEFAULT_TIER = "opus"

# The DB ``ConfigSetting`` key for per-model-id price overrides (§3a #2): a table
# of ``{"<model-id substring>": {"input": <per-MTok>, "output": <per-MTok>}}``
# read via :mod:`teatree.config.cold_reader`, merged BEFORE the built-in
# :data:`PRICE_TABLE` so a swapped model is priced from real numbers instead of
# the conservative opus fallback. Set with
# ``t3 <overlay> config_setting set cost_model_prices '{"deepseek/": {"input": 0.5, "output": 1.5}}'``.
_COST_MODEL_PRICES_KEY = "cost_model_prices"

# Capability order, weakest to strongest, for the per-skill model floor merge
# (:func:`teatree.agents.model_tiering.resolve_spawn_model`). Expressed in the
# ABSTRACT tiers (``cheap`` < ``balanced`` < ``frontier``) — the
# :data:`teatree.agents.model_tiering.TIER_MODELS` keys. Distinct from pricing:
# an unknown full id ranks ABOVE every known tier here (treated as most-capable
# so a below-floor never silently downgrades a spawn), whereas
# :func:`tier_of_model` prices an unknown id at the conservative tier.
_CAPABILITY_ORDER: tuple[str, ...] = ("cheap", "balanced", "frontier")

# The underlying model FAMILY each abstract tier maps to, so :func:`tier_rank`
# ranks an old short-name (``opus``) or a concrete dated id (``claude-opus-4-8``)
# identically to the abstract tier it belongs to (``frontier``). Keyed by the
# family substring; checked after the abstract tier names so an explicit
# ``frontier`` wins without depending on family. PUBLIC — the capability sibling
# ``teatree.agents.model_tiering`` imports it to normalise a resolved Claude id
# back to its abstract tier (it OWNS the abstract tiers but not this mapping).
FAMILY_TO_TIER: dict[str, str] = {"haiku": "cheap", "sonnet": "balanced", "opus": "frontier"}

# A floor whose capability cannot otherwise be inferred defaults to this abstract
# tier rank — the same conservative-reasoning default the prior order used
# (``opus`` ≡ ``frontier``), so ``None``/inherit never downgrades a phase.
_DEFAULT_CAPABILITY_TIER = "frontier"

# Monthly Agent-SDK credit for a Max 20x subscription.
DEFAULT_MONTHLY_CREDIT_USD = 200.0


def tier_of_model(model: str | None) -> str:
    """Normalise a model id / tier name to a :data:`PRICE_TABLE` bucket key.

    Accepts the short tier names (``opus``) and the dated CLI ids
    (``claude-opus-4-8``, optionally suffixed ``[1m]``). ``None`` — an attempt
    that inherited the user's own (uncaptured) default model — is priced at the
    conservative reasoning tier (:data:`_DEFAULT_TIER`). A PRESENT but
    unrecognised id (a swapped non-Claude model) resolves to
    :data:`UNPRICED_TIER` — NOT silently to ``opus`` — so its cost buckets
    honestly instead of masquerading as Claude spend.
    """
    if not model:
        return _DEFAULT_TIER
    lowered = model.lower()
    for tier in _BUILTIN_TIERS:
        if tier in lowered:
            return tier
    return UNPRICED_TIER


def _model_price_from(spec: object) -> ModelPrice | None:
    """A :class:`ModelPrice` from a ``cost_model_prices`` entry, or ``None`` if malformed.

    An entry is ``{"input": <per-MTok>, "output": <per-MTok>}`` (both numeric).
    Anything else — a non-dict, a missing/non-numeric leg — is tolerated and
    dropped (mirrors the ``agent_tier_models`` tolerance) so a malformed override
    never poisons the built-in :data:`PRICE_TABLE`.
    """
    if not isinstance(spec, dict):
        return None
    entry = {str(key): value for key, value in spec.items()}
    input_price = entry.get("input")
    output_price = entry.get("output")
    if isinstance(input_price, bool) or isinstance(output_price, bool):
        return None
    if not isinstance(input_price, int | float) or not isinstance(output_price, int | float):
        return None
    return ModelPrice(input_per_mtok=float(input_price), output_per_mtok=float(output_price))


def _cost_price_overrides() -> dict[str, ModelPrice]:
    """The ``cost_model_prices`` DB overrides: model-id substring → :class:`ModelPrice`.

    Read Django-free via :mod:`teatree.config.cold_reader`; a non-dict value or a
    malformed entry yields no override, so an absent/garbled setting leaves the
    built-in :data:`PRICE_TABLE` untouched. Resolved ONCE per
    :meth:`CostBreakdown.from_usages` and threaded into the per-attempt pricing so
    an aggregation is not N cold reads.
    """
    raw = cold_reader.read_setting(_COST_MODEL_PRICES_KEY)
    if not isinstance(raw, dict):
        return {}
    overrides: dict[str, ModelPrice] = {}
    for pattern, spec in raw.items():
        price = _model_price_from(spec)
        if price is not None:
            overrides[str(pattern)] = price
    return overrides


def price_for_model(model: str | None, *, overrides: Mapping[str, ModelPrice] | None = None) -> ModelPrice:
    """Return the :class:`ModelPrice` for a model id / tier name.

    A ``cost_model_prices`` *overrides* entry whose model-id substring matches
    *model* wins (real numbers for a swapped model); otherwise the built-in
    :data:`PRICE_TABLE` bucket (:func:`tier_of_model`), where an unrecognised id
    lands on the conservative :data:`UNPRICED_TIER`. *overrides* defaults to a
    fresh :func:`_cost_price_overrides` read for a standalone caller; an
    aggregation passes a once-resolved map.
    """
    resolved = _cost_price_overrides() if overrides is None else overrides
    if model and resolved:
        lowered = model.lower()
        for pattern, price in resolved.items():
            if pattern.lower() in lowered:
                return price
    return PRICE_TABLE[tier_of_model(model)]


def tier_rank(model: str | None) -> int:
    """Capability rank of a model id / tier name, for the per-skill floor merge.

    Ranks against :data:`_CAPABILITY_ORDER` (``cheap`` 0 < ``balanced`` <
    ``frontier``). A value is recognised two ways, in order: an abstract tier
    name (``frontier``), or a model FAMILY (``opus`` short-name or a dated
    ``claude-opus-4-8`` id, mapped via :data:`FAMILY_TO_TIER`). ``None`` and the
    inherit sentinels (empty string) rank as :data:`_DEFAULT_CAPABILITY_TIER`
    (``frontier``) so a floor below the inherited default never silently
    downgrades a phase. An unrecognised full id ranks ABOVE every known tier
    (most-capable), the opposite of :func:`tier_of_model`'s conservative pricing
    fallback: an unknown spawn target is assumed strong so a lower floor never
    wins over it.
    """
    if not model:
        return _CAPABILITY_ORDER.index(_DEFAULT_CAPABILITY_TIER)
    lowered = model.lower()
    for rank, tier in enumerate(_CAPABILITY_ORDER):
        if tier in lowered:
            return rank
    for family, tier in FAMILY_TO_TIER.items():
        if family in lowered:
            return _CAPABILITY_ORDER.index(tier)
    return len(_CAPABILITY_ORDER)


# GitHub's agentic-workflow token-efficiency formula (souliane/teatree#657):
# ET = m * (1.0*input + 0.1*cache_read + 4.0*output). ``m`` re-expresses the
# same tier PRICE_TABLE prices at, as GitHub's dimensionless weight, so ET
# figures are comparable to the source article's. Cache-WRITE tokens are
# deliberately excluded — the formula only counts I/C(read)/O.
# Mirrors :data:`PRICE_TABLE`'s key set 1:1 (asserted by
# ``tests/teatree_core/test_cost.py`` — the direct index in
# :func:`compute_effective_tokens` fails LOUDLY the day they drift). The
# :data:`UNPRICED_TIER` bucket takes the CONSERVATIVE opus weight, so a swapped
# model's ET is never understated.
ET_MODEL_MULTIPLIER: dict[str, float] = {
    "opus": 1.0,
    "sonnet": 0.2,
    "haiku": 0.05,
    UNPRICED_TIER: 1.0,
}

# The Layer-2 lane (souliane/teatree#2887) a dispatch's usage is unattributable
# to — no explicit ``agent_harness_provider`` pin was configured, so the
# ambient-credential default authenticated however the ``claude`` CLI's own
# login state resolved (see ``teatree.agents.headless._resolve_dispatch_lane``).
UNATTRIBUTED_LANE = "unattributed"

# The phase bucket for a usage whose phase was never captured (#3157 E2d).
UNATTRIBUTED_PHASE = "unattributed"


@dataclass(frozen=True, slots=True)
class AttemptUsage:
    """The usage fields one :class:`TaskAttempt` carries for costing."""

    model: str | None
    reported_cost_usd: float | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    # The Layer-2 lane (``"subscription"`` / ``"metered"``) this usage
    # authenticated through, or ``""`` when the dispatch carried no explicit
    # Layer-2 pin (see ``TaskAttempt.Lane`` / ``UNATTRIBUTED_LANE``).
    lane: str = ""
    # #3157 E5: whether ``reported_cost_usd`` is a price-table ESTIMATE rather than a
    # real reported (CLI/SDK/router) figure. Threaded from ``TaskAttempt.cost_is_estimated``
    # so ``t3 cost`` can flag estimated spend distinctly from vetted billed cost.
    estimated: bool = False
    # #3157 E2d: the normalized phase this attempt ran, so the cache-hit ratio can be
    # split per phase. ``""`` when the phase was not captured (bucketed as unattributed).
    phase: str = ""

    @property
    def effective_tokens(self) -> float:
        """GitHub's ET formula for this usage (souliane/teatree#657)."""
        return compute_effective_tokens(self)

    @property
    def cacheable_input_tokens(self) -> int:
        """Every input token that was cacheable — served from cache, freshly written, or uncached.

        The denominator of the cache-hit ratio (#3157 E2d): cache reads + cache writes +
        uncached input. Zero when nothing was captured, so the ratio degrades to 0.0.
        """
        return self.cache_read_tokens + self.cache_write_tokens + self.input_tokens


def compute_effective_tokens(usage: AttemptUsage) -> float:
    """GitHub's agentic-workflow ET formula: ``m*(1.0*I + 0.1*C + 4.0*O)``.

    ``tier_of_model`` only ever returns a :data:`PRICE_TABLE` key, and
    :data:`ET_MODEL_MULTIPLIER` mirrors that same key set 1:1, so a direct
    index is safe — and fails loudly (not a silently-wrong multiplier) the
    day the two dicts are ever allowed to drift apart.
    """
    multiplier = ET_MODEL_MULTIPLIER[tier_of_model(usage.model)]
    return multiplier * (1.0 * usage.input_tokens + 0.1 * usage.cache_read_tokens + 4.0 * usage.output_tokens)


def price_table_cost_usd(usage: AttemptUsage, *, overrides: Mapping[str, ModelPrice] | None = None) -> float:
    """Estimate an attempt's cost from the price table (no CLI cost used).

    *overrides* threads a once-resolved ``cost_model_prices`` map through so a
    swapped model prices from its configured numbers; ``None`` reads the DB.
    """
    return price_for_model(usage.model, overrides=overrides).cost(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


def attempt_cost_usd(usage: AttemptUsage, *, overrides: Mapping[str, ModelPrice] | None = None) -> float:
    """SDK-equivalent dollar cost for one attempt.

    Prefers the CLI-reported ``total_cost_usd`` when present (the most
    accurate figure — it already reflects the exact per-iteration rates);
    falls back to the price-table estimate when absent, so historical rows
    whose cost was never captured still contribute an estimate. *overrides*
    threads a once-resolved ``cost_model_prices`` map through to that fallback.
    """
    if usage.reported_cost_usd is not None:
        return usage.reported_cost_usd
    return price_table_cost_usd(usage, overrides=overrides)


class _CacheAccumulator:
    """Accumulates cache reads vs cacheable input per key, to a hit-ratio map (#3157 E2d).

    The cache-hit ratio is ``cache_read / (cache_read + cache_write + input)`` — the fraction
    of cacheable input served from cache. Aggregated across a key's attempts (not averaged
    per attempt) so a few large cached prefixes are weighted correctly; a key with no
    cacheable input at all is omitted rather than reported as a misleading 0%.
    """

    def __init__(self) -> None:
        self._reads: dict[str, int] = {}
        self._cacheable: dict[str, int] = {}

    def add(self, key: str, usage: "AttemptUsage") -> None:
        self._reads[key] = self._reads.get(key, 0) + usage.cache_read_tokens
        self._cacheable[key] = self._cacheable.get(key, 0) + usage.cacheable_input_tokens

    def ratios(self) -> dict[str, float]:
        return {key: self._reads[key] / cacheable for key, cacheable in self._cacheable.items() if cacheable > 0}


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Cycle-to-date SDK-equivalent spend, totalled and split per tier and per Layer-2 lane."""

    total_usd: float = 0.0
    per_tier_usd: dict[str, float] = field(default_factory=dict)
    attempts: int = 0
    # souliane/teatree#657: GitHub's ET metric alongside the dollar figure, and
    # the same totals split by Layer-2 lane (subscription vs metered) so the
    # two-lane cost strategy locked in #2565 is observable.
    effective_tokens_total: float = 0.0
    per_lane_usd: dict[str, float] = field(default_factory=dict)
    per_lane_effective_tokens: dict[str, float] = field(default_factory=dict)
    # #3157 E5: how much of ``total_usd`` is a price-table ESTIMATE (vs a real reported
    # figure), so ``t3 cost`` flags a factory's estimated spend distinctly.
    estimated_usd: float = 0.0
    # #3157 E2d: the cache-hit ratio (cache reads / cacheable input tokens) split per
    # Layer-2 lane and per phase, so a broken cache (a lane/phase stuck at 0%) is visible.
    per_lane_cache_hit_ratio: dict[str, float] = field(default_factory=dict)
    per_phase_cache_hit_ratio: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_usages(cls, usages: Iterable[AttemptUsage]) -> "CostBreakdown":
        # Resolve the ``cost_model_prices`` overrides ONCE for the whole
        # aggregation rather than a cold read per attempt.
        overrides = _cost_price_overrides()
        per_tier: dict[str, float] = {}
        per_lane_usd: dict[str, float] = {}
        per_lane_et: dict[str, float] = {}
        lane_cache = _CacheAccumulator()
        phase_cache = _CacheAccumulator()
        total = 0.0
        estimated = 0.0
        et_total = 0.0
        count = 0
        for usage in usages:
            cost = attempt_cost_usd(usage, overrides=overrides)
            et = usage.effective_tokens
            tier = tier_of_model(usage.model)
            lane = usage.lane or UNATTRIBUTED_LANE
            per_tier[tier] = per_tier.get(tier, 0.0) + cost
            per_lane_usd[lane] = per_lane_usd.get(lane, 0.0) + cost
            per_lane_et[lane] = per_lane_et.get(lane, 0.0) + et
            lane_cache.add(lane, usage)
            phase_cache.add(usage.phase or UNATTRIBUTED_PHASE, usage)
            total += cost
            estimated += cost if usage.estimated else 0.0
            et_total += et
            count += 1
        return cls(
            total_usd=total,
            per_tier_usd=per_tier,
            attempts=count,
            effective_tokens_total=et_total,
            per_lane_usd=per_lane_usd,
            per_lane_effective_tokens=per_lane_et,
            estimated_usd=estimated,
            per_lane_cache_hit_ratio=lane_cache.ratios(),
            per_phase_cache_hit_ratio=phase_cache.ratios(),
        )


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
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

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
            f"  effective tokens (ET): {self.breakdown.effective_tokens_total:,.0f}",
        ]
        if self.breakdown.per_tier_usd:
            lines.append("  per model:")
            for tier, amount in sorted(self.breakdown.per_tier_usd.items(), key=lambda kv: -kv[1]):
                # An unrecognised model with no ``cost_model_prices`` override is
                # billed at the conservative opus rate — flag the figure as an
                # estimate rather than presenting it as a vetted Claude price.
                annotation = (
                    " (unrecognised model — est. at opus rate; set cost_model_prices)"
                    if (tier == UNPRICED_TIER)
                    else ""
                )
                lines.append(f"    {tier}: ${amount:,.2f}{annotation}")
        if self.breakdown.estimated_usd:
            est = self.breakdown.estimated_usd
            est_pct = (est / spent * 100) if spent else 0.0
            lines.append(f"  estimated (price-table, not reported): ${est:,.2f} ({est_pct:.0f}% of spend)")
        if self.breakdown.per_lane_usd:
            lines.append("  per lane:")
            for lane, amount in sorted(self.breakdown.per_lane_usd.items(), key=lambda kv: -kv[1]):
                et = self.breakdown.per_lane_effective_tokens.get(lane, 0.0)
                hit = self.breakdown.per_lane_cache_hit_ratio.get(lane)
                cache = f", cache-hit {hit * 100:.0f}%" if hit is not None else ""
                lines.append(f"    {lane}: ${amount:,.2f} (ET {et:,.0f}{cache})")
        if self.breakdown.per_phase_cache_hit_ratio:
            lines.append("  cache-hit per phase:")
            for phase, ratio in sorted(self.breakdown.per_phase_cache_hit_ratio.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {phase}: {ratio * 100:.0f}%")
        return lines


def register_cost_factories() -> None:
    """Register the cost dataclasses the ``TaskAttempt`` queryset reaches DOWN to (#2385).

    ``cost.py`` depends ON the models indirectly and must NOT move into
    ``modelkit`` (it would break PR-1's ``depends_on == []`` leaf test), so the
    models can't import it without an intra-core up-edge. The registry inverts
    the edge: the model fetches ``AttemptUsage`` / ``CostBreakdown`` by name at
    call time.
    """
    from teatree.core.modelkit.gate_registry import register  # noqa: PLC0415 — deferred: call-time import, kept lazy

    register("cost", "AttemptUsage", AttemptUsage)
    register("cost", "CostBreakdown", CostBreakdown)
