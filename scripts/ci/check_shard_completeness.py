"""Fail loud unless the pytest-split shards form an exact, complete partition.

Each shard writes a small JSON via ``scripts/ci/shard_stats_plugin.py`` recording
the FULL collected count (identical across shards) and the count SELECTED into
that shard's group, alongside which slice of how many it ran. The combiner runs
this checker over every shard-stats file BEFORE combining coverage, because the
whole-tree 93% floor is only honest if every test ran in exactly one shard.
Three failure classes a green coverage number can hide, all caught here: a shard
that silently collected/selected nothing (dropped tests) makes the selected
counts sum to LESS than the total; a duplicated group (two shards ran the same
slice) makes them sum to MORE; and a mis-split whose counts happen to balance is
caught by the group set, which must be an exact ``1..splits`` on one agreed
split count.

Summing is necessary and NOT sufficient. Zero is the number a stats file carries
when nothing measured it, and ``0 == 0`` satisfies the sum on its own — so an
all-zero set of stats would report an exact partition of an empty suite. An empty
slice can also hide inside a matching sum (``[50, 50, 0, 0]`` against a total of
100). Both are the silently-empty shard this check exists to catch, so a shard
that collected nothing and a shard that selected nothing are each their own
failure, named per shard.

Exit 0 only on an exact partition; exit 1 on any problem; exit 2 on misuse.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ShardStats:
    path: Path
    total_collected: int
    selected: int
    group: int | None
    splits: int | None

    @property
    def label(self) -> str:
        return f"{self.path.name} (group {self.group})" if self.group is not None else self.path.name


def _parse(path: Path) -> ShardStats | str:
    if not path.exists():
        return f"shard-stats file missing: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"shard-stats file unreadable: {path} ({exc})"
    try:
        return ShardStats(
            path=path,
            total_collected=int(data["total_collected"]),
            selected=int(data["selected"]),
            group=data.get("group"),
            splits=data.get("splits"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return f"shard-stats file malformed: {path} ({exc})"


def _slice_coverage_problems(parsed: list[ShardStats]) -> list[str]:
    """Whether the shards' own group/splits metadata proves every slice ran once."""
    splits = [shard.splits for shard in parsed if shard.splits is not None]
    groups = [shard.group for shard in parsed if shard.group is not None]
    if len(splits) != len(parsed) or len(groups) != len(parsed):
        return [
            (
                "shard-stats omit `group`/`splits` — which slice of how many each shard ran is "
                "exactly what proves every slice was covered"
            ),
        ]
    if len(set(splits)) != 1:
        return [f"shards disagree on the split count: {sorted(set(splits))}"]
    if sorted(groups) != list(range(1, splits[0] + 1)):
        return [
            (
                f"shard groups {sorted(groups)} are not an exact 1..{splits[0]} set — "
                "a slice was duplicated, skipped, or its stats never arrived"
            ),
        ]
    return []


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

    unmeasured = [shard.label for shard in parsed if shard.total_collected == 0]
    if unmeasured:
        problems.append(
            f"shards report 0 tests collected: {', '.join(unmeasured)} — nothing measured them, "
            f"so they prove no partition (0 == 0 is not a pass)",
        )

    empty = [shard.label for shard in parsed if shard.selected == 0]
    if empty:
        problems.append(
            f"shards selected 0 tests: {', '.join(empty)} — a slice that ran nothing is the "
            f"silently-missing shard this check exists to catch",
        )

    totals = {shard.total_collected for shard in parsed}
    if len(totals) != 1:
        problems.append(f"shards disagree on total collected: {sorted(totals)}")

    groups = [shard.group for shard in parsed if shard.group is not None]
    if len(groups) != len(set(groups)):
        problems.append(f"duplicate group index across shards: {sorted(groups)}")

    problems.extend(_slice_coverage_problems(parsed))

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
