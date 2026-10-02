"""Merge the shards' recorded durations and decide whether to refresh.

Each shard on a refresh run stores its group's fresh, tests-that-ran durations
(``pytest --store-durations --clean-durations``) and uploads the file. This script unions
the per-shard slices back into ``dev/.test_durations`` and decides — from the drift versus
the committed file — whether that refresh is worth a PR. Without a drift gate every daily
run would open a churn PR from pure timing jitter; the gate opens one only when the set of
tests changed (added/removed — the decisive staleness signal) or the aggregate per-test
time moved beyond a threshold.

#4603: a leg that failed, timed out or was cancelled is expected, not fatal. Recording is a
measurement, not a verdict, and stale durations are what unbalance the split that then reds
a leg — so refusing to merge on a red lane is the loop that keeps the durations stale. Two
shapes contribute nothing and are NAMED rather than merged: an absent artifact (the leg died
before the upload), and one byte-equal to the committed baseline (the leg died before pytest
rewrote the file, so publishing it would feed month-old timings back as freshly measured).

What must never happen is a TRUNCATED file, because a test missing from it is bin-packed at
the average and lands wherever collection puts it. So the fresh slices are layered OVER the
committed baseline instead of replacing it: no leg's absence, and no leg that died part-way
through its group, can remove a test that is still in the tree. The cost is that a RENAMED
node id inside a still-present file lingers — harmless to the split, which only ever looks
up ids it collected — while the tree itself prunes the decisive case, a key whose file is gone.

Exit 0 on a merge that contributed something; the refresh verdict is emitted as
``refresh=...`` to ``$GITHUB_OUTPUT`` (and stdout) for the workflow to gate the PR-open step
on. Exit 1 when NO leg recorded anything, which means the upload broke rather than the lane
being red — the case #4584 was really about.
"""

import dataclasses
import json
import os
import sys
from pathlib import Path

DEFAULT_DRIFT_RATIO_THRESHOLD = 0.15
_MIN_POSITIONAL_ARGS = 2  # a durations path + at least one shard file
_ROOT_FLAG = "--root"


@dataclasses.dataclass(frozen=True)
class ShardMerge:
    """The union of the legs that recorded, plus the legs that contributed nothing."""

    fresh: dict[str, float]
    missing: list[Path]
    unrecorded: list[Path]


def load_durations(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): float(v) for k, v in data.items()}


def merge_durations(paths: list[Path], *, baseline: dict[str, float]) -> ShardMerge:
    """Union the shard slices that actually recorded, naming the legs that did not."""
    fresh: dict[str, float] = {}
    missing: list[Path] = []
    unrecorded: list[Path] = []
    for path in paths:
        if not path.exists():
            missing.append(path)
            continue
        recorded = load_durations(path)
        if recorded == baseline:
            unrecorded.append(path)
            continue
        fresh.update(recorded)
    return ShardMerge(fresh=fresh, missing=missing, unrecorded=unrecorded)


def prune_absent_files(durations: dict[str, float], *, root: Path) -> tuple[dict[str, float], list[str]]:
    """Drop node ids whose file is gone from the tree — the tree is the authority."""
    kept: dict[str, float] = {}
    dropped: list[str] = []
    for node_id, seconds in durations.items():
        if (root / node_id.split("::", 1)[0]).is_file():
            kept[node_id] = seconds
        else:
            dropped.append(node_id)
    return kept, sorted(dropped)


@dataclasses.dataclass(frozen=True)
class RefreshDecision:
    should_refresh: bool
    reason: str
    added: int
    removed: int
    drift_ratio: float


def decide_refresh(
    committed: dict[str, float],
    merged: dict[str, float],
    *,
    drift_ratio_threshold: float = DEFAULT_DRIFT_RATIO_THRESHOLD,
) -> RefreshDecision:
    """Refresh when tests were added/removed, or aggregate per-test time drifted past the threshold."""
    added = sorted(set(merged) - set(committed))
    removed = sorted(set(committed) - set(merged))
    shared = set(committed) & set(merged)
    committed_total = sum(committed.values()) or 1.0
    abs_drift = sum(abs(merged[k] - committed[k]) for k in shared)
    drift_ratio = abs_drift / committed_total

    if added or removed:
        return RefreshDecision(
            should_refresh=True,
            reason=f"test set changed: +{len(added)} / -{len(removed)}",
            added=len(added),
            removed=len(removed),
            drift_ratio=drift_ratio,
        )
    if drift_ratio > drift_ratio_threshold:
        return RefreshDecision(
            should_refresh=True,
            reason=f"aggregate duration drift {drift_ratio:.1%} > {drift_ratio_threshold:.0%}",
            added=0,
            removed=0,
            drift_ratio=drift_ratio,
        )
    return RefreshDecision(
        should_refresh=False,
        reason=f"within threshold (drift {drift_ratio:.1%}, no test-set change)",
        added=0,
        removed=0,
        drift_ratio=drift_ratio,
    )


def write_durations(path: Path, durations: dict[str, float]) -> None:
    path.write_text(json.dumps(durations, sort_keys=True, indent=4) + "\n", encoding="utf-8")


def _emit_output(refresh: bool) -> None:
    line = f"refresh={'true' if refresh else 'false'}"
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@dataclasses.dataclass(frozen=True)
class _Invocation:
    durations_path: Path
    shard_paths: list[Path]
    root: Path


def _parse_args(args: list[str]) -> _Invocation | None:
    positional: list[str] = []
    root = Path()
    remaining = list(args)
    while remaining:
        token = remaining.pop(0)
        if token == _ROOT_FLAG:
            if not remaining:
                return None
            root = Path(remaining.pop(0))
        else:
            positional.append(token)
    if len(positional) < _MIN_POSITIONAL_ARGS:
        return None
    return _Invocation(Path(positional[0]), [Path(p) for p in positional[1:]], root)


def main(argv: list[str] | None = None) -> int:
    invocation = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if invocation is None:
        print(
            f"usage: durations_refresh.py [{_ROOT_FLAG} <repo-root>] <durations-path> <shard-durations.json> ...",
            file=sys.stderr,
        )
        return 2

    committed = load_durations(invocation.durations_path)
    merge = merge_durations(invocation.shard_paths, baseline=committed)
    for path in merge.missing:
        print(f"warning: no artifact from {path} — that leg uploaded nothing.", file=sys.stderr)
    for path in merge.unrecorded:
        print(f"warning: {path} is the committed file verbatim — that leg recorded nothing.", file=sys.stderr)

    if not merge.fresh:
        print(
            "error: no leg recorded any durations, so there is nothing measured to publish. A red "
            "lane still records what it ran, so an empty union means the store/upload broke (#4584) — "
            "check the durations-shard-* artifacts. Leaving the committed file untouched.",
            file=sys.stderr,
        )
        return 1

    union = {**committed, **merge.fresh}
    merged, dropped = prune_absent_files(union, root=invocation.root)
    if not merged:
        # Every key resolved to a missing file, which no real tree does — the root is wrong,
        # and publishing an empty durations file would blind the split completely.
        print(
            f"warning: no recorded file resolves under {invocation.root.resolve()}, so the prune is "
            "skipped as unresolvable rather than emptying the durations file.",
            file=sys.stderr,
        )
        merged, dropped = union, []

    decision = decide_refresh(committed, merged)
    delivered = len(invocation.shard_paths) - len(merge.missing) - len(merge.unrecorded)
    print(
        f"Merged {delivered}/{len(invocation.shard_paths)} shard files: {len(committed)} -> {len(merged)} tests "
        f"({len(dropped)} keys pruned as absent from the tree). Refresh: {decision.should_refresh} "
        f"({decision.reason})."
    )
    if decision.should_refresh:
        write_durations(invocation.durations_path, merged)
    _emit_output(decision.should_refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
