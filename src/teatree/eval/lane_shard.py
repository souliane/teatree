r"""Shard a metered eval lane into budget-safe job legs (souliane/teatree#2492).

The metered weekly lane runs each scenario at 3 real metered ``claude`` SDK
trials under a per-job ``2 x 80min`` budget. The catalog is NOT evenly split
across lanes: ``under_load`` is ~14 scenarios (proven to fit the budget) but
``clean_room`` is ~167 — "the whole suite minus under_load". A single
``clean_room`` leg is ~92% of the catalog and very plausibly hits the same
80min wall the undifferentiated full suite hit. Fanning out ONE leg per *lane*
is therefore not enough.

The fix is a second matrix dimension: each lane is partitioned into N contiguous
shards of at most :data:`MAX_SCENARIOS_PER_SHARD` scenarios, so EVERY emitted job
meters a bounded subset that fits the budget. ``under_load`` (~14) stays a single
shard; ``clean_room`` (~167) becomes ceil(167 / bound) shards.

The partition is a deterministic slice of the lane's scenarios SORTED by name
(scenario names are unique — :func:`teatree.eval.discovery._reject_duplicate_names`
enforces it), so the shards are a clean partition: every scenario lands in
exactly one shard, none dropped, none duplicated. :func:`filter_specs_by_shard`
is the single chokepoint the ``t3 eval run --shard`` flag and the CI matrix both
resolve through, so a leg's subset is identical to what its planned shard claims.
"""

import dataclasses
import math

from teatree.eval.models import PERMITTED_LANES, EvalSpec

#: The budget-safe per-shard scenario ceiling. ``under_load`` (~14 scenarios x 3
#: trials) is the one lane PROVEN to finish inside the ``2 x 80min`` budget, so a
#: shard no larger than this is conservatively within budget. Every emitted job
#: meters at most this many scenarios; the matrix splits any larger lane into
#: enough shards to honour it. Keep it at/below the proven ``under_load`` size.
MAX_SCENARIOS_PER_SHARD = 14

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


def shard_count_for(scenario_count: int) -> int:
    """How many budget-safe shards a lane of *scenario_count* scenarios needs."""
    if scenario_count <= 0:
        return 1
    return math.ceil(scenario_count / MAX_SCENARIOS_PER_SHARD)


def plan_lane_shards(specs: list[EvalSpec], lanes: list[str]) -> list[LaneShard]:
    """Plan the budget-safe ``{lane, shard}`` matrix legs for *lanes*.

    For each lane in *lanes*, count its scenarios in *specs* and split it into
    ``ceil(count / MAX_SCENARIOS_PER_SHARD)`` contiguous shards, so every emitted
    leg meters at most :data:`MAX_SCENARIOS_PER_SHARD` scenarios. A lane that
    already fits emits a single ``1/1`` leg.
    """
    legs: list[LaneShard] = []
    for lane in lanes:
        if lane not in PERMITTED_LANES:
            permitted = ", ".join(sorted(PERMITTED_LANES))
            msg = f"unknown lane {lane!r}; permitted lanes: {permitted}"
            raise ShardSpecError(msg)
        count = sum(1 for spec in specs if spec.lane == lane)
        total = shard_count_for(count)
        legs.extend(LaneShard(lane=lane, index=index, total=total) for index in range(1, total + 1))
    return legs
