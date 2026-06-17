r"""Emit the eval-lane matrix JSON for the metered workflow (#2492).

The full metered suite (~68 scenarios x 3 trials, each a real metered ``claude``
SDK call) does not fit the per-job ``2 x 80min`` budget. The fix is to fan the
run out across lanes so each leg meters ONE lane (``clean_room`` / ``under_load``)
that fits, in parallel job legs. This script computes the per-leg lane list a
GitHub Actions ``strategy.matrix`` consumes via ``fromJSON``.

An empty ``--lane`` (the scheduled weekly run, and the default manual run) maps to
every permitted lane, one matrix leg each — full coverage, parallel wall-clock. An
explicit ``--lane <name>`` (a targeted manual run) maps to that single lane.

The matrix is emitted as a JSON array of lane strings to stdout, ready for
``echo "lanes=$(...)" >> "$GITHUB_OUTPUT"``. An unknown explicit lane exits
non-zero with the permitted set, so a typo fails loud rather than running an
empty matrix.
"""

import argparse
import json
import sys

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", default="", help="A single lane to run; empty = every permitted lane.")
    args = parser.parse_args(argv)
    print(json.dumps(_lanes_for(args.lane)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
