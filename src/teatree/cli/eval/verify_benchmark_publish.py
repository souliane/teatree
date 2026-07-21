"""``t3 eval verify-benchmark-publish`` — the dashboard publish gate.

The weekly workflow's publish job runs this over the collected shard artifacts
BEFORE it commits them. A shard whose matrix records graded verdicts against zero
metered cost is the exhausted-OAuth-window signature; the whole publish is
refused (exit 1) rather than the shard being dropped from an otherwise
complete-looking dashboard. The policy lives in
:mod:`teatree.eval.benchmark_publish_guard` so it is testable without CI.
"""

from pathlib import Path

import typer

from teatree.eval.benchmark_publish_guard import SHARD_GLOB, UnmeteredShardError, verify_publishable


def verify_benchmark_publish(
    dashboard_dir: Path = typer.Argument(..., help="Directory holding the collected eval-benchmark-*.html shards."),
) -> None:
    """Exit 1 when any collected benchmark shard is not backed by real metered spend."""
    try:
        verify_publishable(dashboard_dir)
    except UnmeteredShardError as exc:
        typer.echo(f"::error::{exc}", err=True)
        raise SystemExit(1) from exc
    count = len(list(dashboard_dir.glob(SHARD_GLOB)))
    typer.echo(f"{count} benchmark shard(s) are backed by metered spend → publishable")
