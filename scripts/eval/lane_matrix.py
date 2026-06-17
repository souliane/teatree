r"""Emit the eval-lane SHARD matrix JSON for the metered workflow (#2492).

Each metered job runs its scenarios at 3 real ``claude`` SDK trials under a
``2 x 80min`` budget. The catalog is NOT evenly split across lanes: ``under_load``
is ~14 scenarios (proven to fit) but ``clean_room`` is ~167 — "the whole suite
minus under_load", ~92% of the catalog. A single ``clean_room`` leg would very
plausibly hit the same 80min wall the undifferentiated full suite hit, so fanning
out one leg per *lane* is not enough.

This script therefore emits a ``{lane, shard}`` matrix: each lane is split into
``ceil(count / MAX_SCENARIOS_PER_SHARD)`` contiguous shards (a deterministic
partition by scenario name — every scenario in exactly one shard, none dropped or
duplicated), so EVERY emitted job meters a budget-safe subset. ``under_load``
(~14) stays one shard; ``clean_room`` (~167) becomes several. Counts are read
from the LIVE catalog, so the split is never stale.

An empty ``--lane`` (the scheduled weekly run, and the default manual run) maps
to every permitted lane, sharded. An explicit ``--lane <name>`` shards only that
lane. The matrix is emitted as a JSON array of ``{"lane", "shard"}`` objects to
stdout, ready for ``echo "include=$(...)" >> "$GITHUB_OUTPUT"``. An unknown
explicit lane exits non-zero with the permitted set, so a typo fails loud rather
than running an empty matrix.
"""

import argparse
import json
import sys

from teatree.eval.discovery import discover_specs
from teatree.eval.lane_shard import plan_lane_shards
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


def _matrix_for(requested: str) -> list[dict[str, str]]:
    lanes = _lanes_for(requested)
    legs = plan_lane_shards(discover_specs(), lanes)
    return [leg.as_matrix_entry() for leg in legs]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", default="", help="A single lane to run; empty = every permitted lane.")
    args = parser.parse_args(argv)
    print(json.dumps(_matrix_for(args.lane)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
