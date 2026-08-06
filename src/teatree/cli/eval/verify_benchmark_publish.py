"""``t3 eval verify-benchmark-publish`` — the dashboard publish gate.

The weekly workflow's publish job runs this over the collected shard artifacts
BEFORE it commits them. Two ways a dashboard is unpublishable, and the job hits
both from the same ``if: always()`` trigger: a shard whose matrix records graded
verdicts against zero metered cost (the exhausted-OAuth-window signature), and a
shard that is simply ABSENT because its leg timed out and uploaded nothing.
``--expected-shards`` is the matrix leg count the run planned, so the second case
is a refusal rather than a smaller dashboard that looks complete. Either way the
whole publish is refused (exit 1). The policy lives in
:mod:`teatree.eval.benchmark_publish_guard` so it is testable without CI.
"""

from pathlib import Path

import typer

from teatree.eval.benchmark_publish_guard import (
    IncompleteDashboardError,
    UnmeteredShardError,
    shard_paths,
    verify_publishable,
)


def verify_benchmark_publish(
    dashboard_dir: Path = typer.Argument(..., help="Directory holding the collected eval-benchmark-*.html shards."),
    expected_shards: int = typer.Option(
        ...,
        "--expected-shards",
        help="Matrix leg count the run planned; fewer collected artifacts refuses the publish.",
    ),
) -> None:
    """Exit 1 when the collected dashboard is short a shard or not backed by metered spend."""
    try:
        verify_publishable(dashboard_dir, expected_shards=expected_shards)
    except (IncompleteDashboardError, UnmeteredShardError) as exc:
        typer.echo(f"::error::{exc}", err=True)
        raise SystemExit(1) from exc
    count = len(shard_paths(dashboard_dir))
    typer.echo(f"all {count} planned benchmark shard(s) present and backed by metered spend → publishable")
