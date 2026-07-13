r"""Emit the eval-ci-heal SHARD matrix JSON for the full-suite heal workflow.

The CI-eval heal workflow runs the behavioral eval against a PR branch. The FULL
suite (empty ``scenarios`` input) is ~231 scenarios — far too many for one CI step
to finish inside the per-step timeout, so it is fanned out across a parallel matrix
of ``--shards`` legs. Each leg runs ``t3 eval run --shard <index>/<total>``, a
deterministic partition by scenario name (every scenario in exactly one shard, none
dropped or duplicated — :func:`teatree.eval.lane_shard.filter_specs_by_shard`), so
every leg meters a bounded subset that fits the step budget.

A red-subset re-run (a NON-empty ``scenarios`` input — the reds a heal re-trigger
re-runs) is NOT sharded: it is already a small named set, so it emits a SINGLE leg
with an empty shard token and the eval job runs the per-scenario loop over the
named subset unchanged.

The matrix is emitted as a JSON array of ``{"shard"}`` objects to stdout, ready for
``echo "matrix=$(...)" >> "$GITHUB_OUTPUT"``. This helper imports nothing from
teatree — the shard token slices the LIVE catalog in-container at run time, so the
matrix is a pure function of the shard count and whether a subset was requested.
"""

import argparse
import json
import sys


def shard_matrix(shards: int, scenarios: str) -> list[dict[str, str]]:
    """The ``{"shard"}`` matrix legs for *shards* shards, or a single unsharded leg.

    A non-empty *scenarios* (a red-subset re-run) is never sharded — one leg with
    an empty shard token runs the named subset loop. Otherwise the full suite fans
    out ``shards`` legs, ``1/shards`` .. ``shards/shards``.
    """
    if scenarios.strip():
        return [{"shard": ""}]
    if shards < 1:
        msg = f"--shards must be >= 1, got {shards}"
        raise SystemExit(msg)
    return [{"shard": f"{index}/{shards}"} for index in range(1, shards + 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, required=True, help="Number of parallel shards for the full suite.")
    parser.add_argument(
        "--scenarios",
        default="",
        help="Comma-joined red subset; non-empty = a single unsharded leg (no full-suite fan-out).",
    )
    args = parser.parse_args(argv)
    print(json.dumps(shard_matrix(args.shards, args.scenarios)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
