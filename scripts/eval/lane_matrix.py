r"""Emit the eval-lane SHARD matrix JSON for the metered workflow (#2492, #2683).

Each metered job runs its scenarios at 3 real ``claude`` SDK trials under a
``2 x 80min`` budget. The catalog is NOT evenly split across lanes: ``under_load``
is 14 scenarios but ``clean_room`` is ~182 — "the whole suite minus under_load",
~92% of the catalog. A single ``clean_room`` leg would very plausibly hit the same
80min wall the undifferentiated full suite hit, so fanning out one leg per *lane*
is not enough.

This script therefore emits a ``{lane, shard}`` matrix: each lane is split into
``ceil(count / max_scenarios_per_shard(lane))`` contiguous shards (a deterministic
partition by scenario name — every scenario in exactly one shard, none dropped or
duplicated), so EVERY emitted job meters a budget-safe subset. The per-shard
ceiling is LANE-AWARE (#2683): ``clean_room`` (fast, one skill into an empty
context) uses 14, so ~182 becomes ~13 shards; ``under_load`` (slow — full skill
bundle + polluted preamble + a spawned multi-agent roster, 10-45 min/scenario)
uses 4, so 14 becomes 4 shards. A single under_load ``1/1`` leg hit the 80min cap
in run 27995563148. Counts are read from the LIVE catalog, so the split is never
stale.

An empty ``--lane`` (the scheduled weekly run, and the default manual run) maps
to every permitted lane, sharded. An explicit ``--lane <name>`` shards only that
lane. The matrix is emitted as a JSON array of ``{"lane", "shard"}`` objects to
stdout, ready for ``echo "include=$(...)" >> "$GITHUB_OUTPUT"``. An unknown
explicit lane exits non-zero with the permitted set, so a typo fails loud rather
than running an empty matrix.

MODEL-TIER AXIS (``--efforts``)
    An optional second axis runs each ``{lane, shard}`` leg once per reasoning-
    effort tier. ``--efforts low,medium,high`` multiplies every leg across the
    three tiers and emits ``{"lane", "shard", "effort"}`` objects; the eval job
    passes the leg's ``effort`` through ``t3 eval run --effort <tier>``, so the
    weekly run measures pass-rate vs reasoning effort across the whole suite. Omit
    ``--efforts`` (the default) and the matrix keeps the legacy ``{lane, shard}``
    shape with no effort axis — a clean no-op for the single-tier PR lane. An
    unknown effort fails loud with the known levels.
"""

import argparse
import json
import sys

from teatree.eval.discovery import discover_specs
from teatree.eval.lane_shard import plan_lane_shards
from teatree.eval.model_variant import EFFORT_LEVELS
from teatree.eval.models import PERMITTED_LANES


def _lanes_for(requested: str) -> list[str]:
    requested = requested.strip()
    if not requested:
        return sorted(PERMITTED_LANES)
    if requested not in PERMITTED_LANES:
        permitted = ", ".join(sorted(PERMITTED_LANES))
        msg = f"unknown lane {requested!r}; permitted lanes: {permitted}"
        raise SystemExit(msg)
    return [requested]


def _efforts_for(requested: str) -> list[str | None]:
    """Parse the ``--efforts`` axis; empty → ``[None]`` (no effort axis at all).

    A blank request keeps the legacy single-tier matrix — ``[None]`` is the
    no-axis sentinel, so the leg carries no ``effort`` key. A comma list is
    validated against the known effort levels; an unknown tier fails loud rather
    than emitting a leg the CLI would reject.
    """
    tiers = [tier.strip() for tier in requested.split(",") if tier.strip()]
    if not tiers:
        return [None]
    for tier in tiers:
        if tier not in EFFORT_LEVELS:
            known = ", ".join(EFFORT_LEVELS)
            msg = f"unknown effort {tier!r}; known levels: {known}"
            raise SystemExit(msg)
    return list(tiers)


def _matrix_for(requested: str, *, efforts: str = "") -> list[dict[str, str]]:
    lanes = _lanes_for(requested)
    tiers = _efforts_for(efforts)
    legs = plan_lane_shards(discover_specs(), lanes)
    return [
        ({**leg.as_matrix_entry(), "effort": tier} if tier is not None else leg.as_matrix_entry())
        for tier in tiers
        for leg in legs
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", default="", help="A single lane to run; empty = every permitted lane.")
    parser.add_argument(
        "--efforts",
        default="",
        help="Comma-separated reasoning-effort tiers (e.g. 'low,medium,high'); empty = no effort axis.",
    )
    args = parser.parse_args(argv)
    print(json.dumps(_matrix_for(args.lane, efforts=args.efforts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
