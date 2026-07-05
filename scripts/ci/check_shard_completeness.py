"""Fail loud unless the pytest-split shards form an exact, complete partition.

Each shard writes a small JSON via ``scripts/ci/shard_stats_plugin.py`` recording
the FULL collected count (identical across shards) and the count SELECTED into
that shard's group. The combiner runs this checker over every shard-stats file
BEFORE combining coverage, because the whole-tree 93% floor is only honest if
every test ran in exactly one shard. Two failure classes a green coverage number
can hide, both caught here: a shard that silently collected/selected nothing
(dropped tests) makes the selected counts sum to LESS than the total; a
duplicated group (two shards ran the same slice, another slice never ran) makes
them sum to MORE than the total.

Exit 0 only on an exact partition; exit 1 on any problem; exit 2 on misuse.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ShardStats:
    total_collected: int
    selected: int
    group: int | None
    splits: int | None


def _parse(path: Path) -> ShardStats | str:
    if not path.exists():
        return f"shard-stats file missing: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"shard-stats file unreadable: {path} ({exc})"
    try:
        return ShardStats(
            total_collected=int(data["total_collected"]),
            selected=int(data["selected"]),
            group=data.get("group"),
            splits=data.get("splits"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return f"shard-stats file malformed: {path} ({exc})"


def evaluate(paths: list[Path]) -> tuple[list[str], int | None]:
    """Return (problems, agreed_total). Empty problems means an exact partition."""
    problems: list[str] = []
    parsed: list[ShardStats] = []
    for path in paths:
        result = _parse(path)
        if isinstance(result, str):
            problems.append(result)
        else:
            parsed.append(result)

    if not parsed:
        problems.append("no readable shard-stats files")
        return problems, None

    totals = {shard.total_collected for shard in parsed}
    if len(totals) != 1:
        problems.append(f"shards disagree on total collected: {sorted(totals)}")

    groups = [shard.group for shard in parsed if shard.group is not None]
    if len(groups) != len(set(groups)):
        problems.append(f"duplicate group index across shards: {sorted(groups)}")

    total = min(totals)
    selected_sum = sum(shard.selected for shard in parsed)
    if selected_sum != total:
        problems.append(
            f"selected counts sum to {selected_sum} but total collected is {total} — "
            f"tests were dropped (sum<total) or duplicated (sum>total)",
        )

    return problems, (total if not problems else None)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: check_shard_completeness.py <shard-stats.json> ...", file=sys.stderr)
        return 2

    problems, total = evaluate([Path(arg) for arg in args])
    if problems:
        print("Shard partition check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"Shard partition OK: {len(args)} shards, {total} tests accounted for exactly once.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
