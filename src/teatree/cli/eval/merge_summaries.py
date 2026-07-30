"""``t3 eval merge-summaries`` — the reusable weekly-dashboard merger.

Reads every per-shard sanitized ``--summary-md`` file (a directory or explicit
paths) and renders ONE combined dashboard markdown — counts, total cost, and the
merged ``scenario | lane | verdict | trials | cost`` table sorted by lane then
name. The shared core lives in :mod:`teatree.eval.summaries`; the host's
``scripts/eval/merge_summaries.py`` shim and this overlay-facing CLI both
delegate to it, so an overlay's publish job reuses the merge instead of
duplicating it.

The run-url / sha / generated-at are PASSED IN (the timestamp is never computed
here, so the merge is deterministic). Only the publish-safe summary rows are
read — the transcript never enters here, so the dashboard is safe to commit and
serve on Pages. Writes to ``--out`` when given, else stdout.
"""

from pathlib import Path

import typer

from teatree.eval.summaries import merge_summaries as _merge_summaries


def merge_summaries(
    inputs: list[str] = typer.Argument(..., help="Per-shard summary .md files, or a directory of them."),
    run_url: str = typer.Option(..., "--run-url", help="The workflow run URL (injected by the workflow)."),
    sha: str = typer.Option(..., "--sha", help="The commit SHA the run measured (injected)."),
    generated_at: str = typer.Option(..., "--generated-at", help="ISO-8601 timestamp (injected; never computed here)."),
    out: Path | None = typer.Option(None, "--out", help="Write the dashboard to this path instead of stdout."),
) -> None:
    """Merge per-shard summary markdown into one dashboard (to --out or stdout)."""
    dashboard = _merge_summaries(inputs, run_url=run_url, sha=sha, generated_at=generated_at)
    if out is not None:
        out.write_text(dashboard, encoding="utf-8")
    else:
        typer.echo(dashboard)
