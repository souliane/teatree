"""Say what the shard lane's raised per-test ceiling actually bought (#4048).

The sharded lane runs at a raised ceiling because twelve shards contending on one
runner cost more than the tight local one was sized for. Left there, the raise
would only move the cliff: a test drifting toward the new ceiling is invisible
until the day it times out and reds a PR whose diff never touched it — the class
of red this epic exists to remove.

So every shard records its slowest tests against the ceiling that applied to them
(``scripts/ci/shard_stats_plugin.py``) and this merges the twelve into one list of
what is running out of room. It is a REPORT and never a gate: it exits 0 on a
recorded over-run exactly as on a clean run, because the required context reddening
for a slow test nobody caused is the very thing being fixed. The gate for a test
that has genuinely outgrown its ceiling stays the timeout itself; this is what
makes that foreseeable, alongside the doctor's reading of the same signal off the
committed durations file.

Exit 0 always, except 2 on misuse.
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from teatree.quality.timeout_headroom import TIGHT_FRACTION

_NAMED_LIMIT = 15


@dataclass(frozen=True)
class Pressure:
    node_id: str
    seconds: float
    ceiling: float

    @property
    def consumed(self) -> float:
        return self.seconds / self.ceiling


def collect(paths: list[Path]) -> tuple[list[Pressure], list[str]]:
    """Return the merged (pressured, unreadable) reading across every shard-stats file."""
    pressured: dict[str, Pressure] = {}
    unreadable: list[str] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data["slowest_against_ceiling"]
        except (OSError, json.JSONDecodeError) as exc:
            unreadable.append(f"{path.name}: {exc}")
            continue
        except (KeyError, TypeError):
            continue  # a shard that recorded no headroom (no pytest-timeout) still partitions fine
        for entry in entries:
            pressure = _parse(entry)
            if pressure is None or pressure.consumed < TIGHT_FRACTION:
                continue
            seen = pressured.get(pressure.node_id)
            if seen is None or pressure.consumed > seen.consumed:
                pressured[pressure.node_id] = pressure
    return sorted(pressured.values(), key=lambda pressure: (-pressure.consumed, pressure.node_id)), unreadable


def _parse(entry: object) -> Pressure | None:
    """One entry a shard wrote; anything whose shape does not fit is dropped rather than guessed."""
    if not isinstance(entry, dict):
        return None
    node_id: str | None = None
    seconds: float | None = None
    ceiling: float | None = None
    for key, value in entry.items():
        if key == "node_id" and isinstance(value, str):
            node_id = value
        elif key == "seconds" and isinstance(value, int | float):
            seconds = float(value)
        elif key == "ceiling" and isinstance(value, int | float) and value > 0:
            ceiling = float(value)
    if node_id is None or seconds is None or ceiling is None:
        return None
    return Pressure(node_id=node_id, seconds=seconds, ceiling=ceiling)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: report_timeout_headroom.py <shard-stats.json> ...", file=sys.stderr)
        return 2

    pressured, unreadable = collect([Path(arg) for arg in args])
    for problem in unreadable:
        print(f"Shard-stats file unreadable (headroom not counted from it) — {problem}")

    if not pressured:
        print(f"Timeout headroom: none of the recorded tests reach {TIGHT_FRACTION:.0%} of their ceiling.")
        return 0

    print(f"Timeout headroom: {len(pressured)} test(s) past {TIGHT_FRACTION:.0%} of the ceiling that applies to them.")
    print()
    print("| consumed | seconds | ceiling | test |")
    print("| --- | --- | --- | --- |")
    for pressure in pressured[:_NAMED_LIMIT]:
        print(f"| {pressure.consumed:.0%} | {pressure.seconds:.1f}s | {pressure.ceiling:g}s | `{pressure.node_id}` |")
    if len(pressured) > _NAMED_LIMIT:
        print(f"\n… and {len(pressured) - _NAMED_LIMIT} more.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
