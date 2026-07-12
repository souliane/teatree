"""Merge the four scheduled shards' recorded durations and decide whether to refresh.

Each shard on the daily ``schedule`` run stores its group's fresh, tests-that-ran
durations (``pytest --store-durations --clean-durations``) and uploads the file. This
script unions the four disjoint slices back into one complete ``dev/.test_durations``
and decides — from the drift versus the committed file — whether that refresh is worth
a PR. Without a drift gate every daily run would open a churn PR from pure timing
jitter; the gate opens one only when the set of tests changed (added/removed — the
decisive staleness signal, e.g. the currently-unrecorded ``tests/quality`` + doctests)
or the aggregate per-test time moved beyond a threshold.

Exit 0 always on a successful merge; the refresh verdict is emitted as ``refresh=...``
to ``$GITHUB_OUTPUT`` (and stdout) for the workflow to gate the PR-open step on.
"""

import dataclasses
import json
import os
import sys
from pathlib import Path

DEFAULT_DRIFT_RATIO_THRESHOLD = 0.15
_MIN_ARGS = 2  # a durations path + at least one shard file


class MissingShardDurationsError(FileNotFoundError):
    """An expected per-shard durations file is absent — refuse to write a truncated merge."""


def load_durations(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): float(v) for k, v in data.items()}


def merge_durations(paths: list[Path]) -> dict[str, float]:
    """Union the per-shard duration slices (the four groups partition the suite).

    A missing shard file is a HARD error, never an empty contribution: the four
    groups partition the suite, so an absent shard would silently drop ~1/4 of the
    tests from the merged file and PR a truncated ``dev/.test_durations`` that
    unbalances the shard split. Fail LOUD (raise) so a partial merge never ships —
    an absent artifact means an upload/download failure to investigate, not a
    refresh to publish.
    """
    merged: dict[str, float] = {}
    for path in paths:
        if not path.exists():
            message = (
                f"expected shard-durations file is absent: {path}. The four shards partition "
                "the suite, so a missing shard would silently drop ~1/4 of the tests from the "
                "merged durations — refusing to write a truncated file. Check the "
                "durations-shard-* artifact upload/download in the refresh-durations job."
            )
            raise MissingShardDurationsError(message)
        merged.update(load_durations(path))
    return merged


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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < _MIN_ARGS:
        print("usage: durations_refresh.py <durations-path> <shard-durations.json> ...", file=sys.stderr)
        return 2

    durations_path = Path(args[0])
    shard_paths = [Path(a) for a in args[1:]]

    committed = load_durations(durations_path)
    try:
        merged = merge_durations(shard_paths)
    except MissingShardDurationsError as exc:
        # Fail LOUD: a missing shard would truncate the merge. Do NOT emit a refresh
        # verdict and do NOT touch the committed file — the job step fails so the
        # maintainer investigates the artifact instead of shipping a partial file.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not merged:
        print("No shard durations to merge — nothing recorded.", file=sys.stderr)
        _emit_output(False)
        return 0

    decision = decide_refresh(committed, merged)
    print(
        f"Merged {len(shard_paths)} shard files: {len(committed)} -> {len(merged)} tests. "
        f"Refresh: {decision.should_refresh} ({decision.reason})."
    )
    if decision.should_refresh:
        write_durations(durations_path, merged)
    _emit_output(decision.should_refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
