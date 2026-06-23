r"""Shard a metered eval lane into budget-safe job legs (souliane/teatree#2492, #2683).

The metered weekly lane runs each scenario at 3 real metered ``claude`` SDK
trials under a per-job ``2 x 80min`` budget. The catalog is NOT evenly split
across lanes: ``under_load`` is 14 scenarios but ``clean_room`` is ~182 — "the
whole suite minus under_load". A single ``clean_room`` leg is ~92% of the catalog
and very plausibly hits the same 80min wall the undifferentiated full suite hit.
Fanning out ONE leg per *lane* is therefore not enough.

The fix is a second matrix dimension: each lane is partitioned into N contiguous
shards, so EVERY emitted job meters a bounded subset that fits the budget. The
per-shard ceiling is LANE-AWARE because the two lanes have wildly different
per-scenario cost.

A ``clean_room`` scenario loads ONE skill into an empty context — seconds to a
minute even at 3 trials — so the ceiling is :data:`MAX_SCENARIOS_PER_SHARD` (14)
and ``clean_room`` (~182) becomes ceil(182 / 14) shards.

An ``under_load`` scenario loads the FULL skill bundle, an 8k-20k-token polluted
context preamble, AND spawns a multi-agent roster — 10-45 MINUTES per scenario at
3 trials. So its ceiling is the much smaller
:data:`UNDER_LOAD_MAX_SCENARIOS_PER_SHARD` (4). Run 27995563148 proved the lane
could NOT run as a single ``1/1`` leg: it hit the 80min step cap (the 4800000ms
timeout). The 14-scenario lane now splits into 4 parallel shards.

The partition is a deterministic slice of the lane's scenarios SORTED by name
(scenario names are unique — :func:`teatree.eval.discovery._reject_duplicate_names`
enforces it), so the shards are a clean partition: every scenario lands in
exactly one shard, none dropped, none duplicated. :func:`filter_specs_by_shard`
is the single chokepoint the ``t3 eval run --shard`` flag and the CI matrix both
resolve through, so a leg's subset is identical to what its planned shard claims.
"""

import dataclasses
import math

from teatree.eval.models import PERMITTED_LANES, UNDER_LOAD_LANE, EvalSpec

#: The DEFAULT budget-safe per-shard scenario ceiling — used by ``clean_room`` and
#: any lane without an explicit override. A clean_room scenario loads ONE skill
#: into an empty context, so it runs in seconds-to-a-minute even at 3 trials; ~14
#: such scenarios finish well inside the ``2 x 80min`` budget. Every emitted job
#: meters at most its lane's ceiling; the matrix splits any larger lane into enough
#: shards to honour it.
MAX_SCENARIOS_PER_SHARD = 14

#: The ``under_load`` per-shard ceiling — much smaller than the default because an
#: under_load scenario is FAR more expensive (souliane/teatree#2683). It loads the
#: FULL skill bundle, an 8k-20k-token polluted context preamble, AND spawns a
#: multi-agent roster, so a single scenario runs 10-45 MINUTES at 3 trials. Run
#: 27995563148 proved the lane could not fit a single ``1/1`` leg: 11 of its 14
#: scenarios already burned 70 min and ``full_speed_fans_out_parallel_workers_not_serial``
#: alone took 45 min, so the leg hit the 80min step cap (the 4800000ms timeout).
#: A ceiling of 4 keeps a shard's wall-clock comfortably under 80 min even when the
#: 45-min roster scenario lands in a shard with three short ones, and splits the
#: 14-scenario lane into 4 shards that run in parallel.
UNDER_LOAD_MAX_SCENARIOS_PER_SHARD = 4

#: Per-lane override of the default per-shard ceiling. A lane absent here uses
#: :data:`MAX_SCENARIOS_PER_SHARD`. The single source of truth for the lane-aware
#: split — both the planner (:func:`plan_lane_shards`) and the budget-invariant
#: tests resolve through :func:`max_scenarios_per_shard`.
_LANE_SHARD_CEILINGS: dict[str, int] = {UNDER_LOAD_LANE: UNDER_LOAD_MAX_SCENARIOS_PER_SHARD}


def max_scenarios_per_shard(lane: str) -> int:
    """The budget-safe per-shard scenario ceiling for *lane*.

    ``under_load`` (roster-spawning, 10-45 min/scenario) gets a small ceiling so a
    shard fits the ``2 x 80min`` budget; every other lane uses the default
    :data:`MAX_SCENARIOS_PER_SHARD`. This is the single chokepoint the planner and
    the budget-invariant tests both resolve a lane's ceiling through.
    """
    return _LANE_SHARD_CEILINGS.get(lane, MAX_SCENARIOS_PER_SHARD)


#: A shard token is exactly two ``/``-separated fields: ``index/total``.
_SHARD_FIELD_COUNT = 2


@dataclasses.dataclass(frozen=True)
class LaneShard:
    """One CI matrix leg: a lane plus its 1-based ``index/total`` shard token.

    ``total == 1`` is a whole-lane leg (the lane already fits the budget);
    ``total > 1`` splits the lane. ``shard`` is the ``"index/total"`` string the
    ``t3 eval run --shard`` flag consumes; :meth:`as_matrix_entry` is the JSON
    object the GitHub Actions ``strategy.matrix`` includes.
    """

    lane: str
    index: int
    total: int

    @property
    def shard(self) -> str:
        return f"{self.index}/{self.total}"

    def as_matrix_entry(self) -> dict[str, str]:
        return {"lane": self.lane, "shard": self.shard}


class ShardSpecError(ValueError):
    """A malformed or out-of-range ``--shard``/matrix shard token."""


def parse_shard(shard: str | None) -> tuple[int, int] | None:
    """Parse an ``"index/total"`` shard token into 1-based ``(index, total)``.

    ``None`` or empty → ``None`` (no sharding; the whole lane). A malformed token
    (not ``i/N``, non-positive, or ``index > total``) raises :class:`ShardSpecError`
    so a typo fails loud rather than silently running an empty or wrong subset.
    """
    if shard is None:
        return None
    shard = shard.strip()
    if not shard:
        return None
    parts = shard.split("/")
    if len(parts) != _SHARD_FIELD_COUNT or not all(part.strip().isdigit() for part in parts):
        msg = f"malformed --shard {shard!r}; expected 'index/total' (1-based), e.g. '2/6'"
        raise ShardSpecError(msg)
    index, total = int(parts[0]), int(parts[1])
    if total < 1 or index < 1 or index > total:
        msg = f"out-of-range --shard {shard!r}; need 1 <= index <= total and total >= 1"
        raise ShardSpecError(msg)
    return index, total


def _shard_slice(count: int, index: int, total: int) -> tuple[int, int]:
    """Return the ``[start, stop)`` of the *index*-th of *total* contiguous shards.

    The remainder is spread one-per-shard across the leading shards, so shard
    sizes differ by at most one and the union of all shards is exactly
    ``[0, count)`` — a clean partition with nothing dropped or duplicated.
    """
    base, extra = divmod(count, total)
    start = (index - 1) * base + min(index - 1, extra)
    size = base + (1 if index - 1 < extra else 0)
    return start, start + size


def filter_specs_by_shard(specs: list[EvalSpec], shard: str | None) -> list[EvalSpec]:
    """Return the *index*-th of *total* shards of *specs* (sorted by name).

    ``None``/empty shard → *specs* unchanged. The specs are sorted by their unique
    name first, so the same shard token always selects the same scenarios. This is
    the single chokepoint the ``--shard`` CLI flag and the CI matrix both resolve
    through.
    """
    parsed = parse_shard(shard)
    if parsed is None:
        return specs
    index, total = parsed
    ordered = sorted(specs, key=lambda spec: spec.name)
    start, stop = _shard_slice(len(ordered), index, total)
    return ordered[start:stop]


def shard_count_for(scenario_count: int, lane: str | None = None) -> int:
    """How many budget-safe shards a lane of *scenario_count* scenarios needs.

    The ceiling is *lane*-aware (souliane/teatree#2683): ``under_load`` uses the
    smaller :data:`UNDER_LOAD_MAX_SCENARIOS_PER_SHARD` because its scenarios are
    roster-spawning and 10-45 min each, so the same count splits into MORE shards
    than a clean_room lane of identical size. ``lane=None`` keeps the legacy
    default ceiling for callers that don't carry a lane.
    """
    if scenario_count <= 0:
        return 1
    ceiling = MAX_SCENARIOS_PER_SHARD if lane is None else max_scenarios_per_shard(lane)
    return math.ceil(scenario_count / ceiling)


def plan_lane_shards(specs: list[EvalSpec], lanes: list[str]) -> list[LaneShard]:
    """Plan the budget-safe ``{lane, shard}`` matrix legs for *lanes*.

    For each lane in *lanes*, count its scenarios in *specs* and split it into
    ``ceil(count / max_scenarios_per_shard(lane))`` contiguous shards, so every
    emitted leg meters at most that lane's ceiling. The ceiling is lane-aware
    (#2683): ``under_load`` (roster-spawning, 10-45 min/scenario) gets a smaller
    ceiling than ``clean_room``, so its 14 scenarios split into several shards
    rather than one ``1/1`` leg that hits the 80min cap. A lane that already fits
    its ceiling emits a single ``1/1`` leg.
    """
    legs: list[LaneShard] = []
    for lane in lanes:
        if lane not in PERMITTED_LANES:
            permitted = ", ".join(sorted(PERMITTED_LANES))
            msg = f"unknown lane {lane!r}; permitted lanes: {permitted}"
            raise ShardSpecError(msg)
        count = sum(1 for spec in specs if spec.lane == lane)
        total = shard_count_for(count, lane)
        legs.extend(LaneShard(lane=lane, index=index, total=total) for index in range(1, total + 1))
    return legs
