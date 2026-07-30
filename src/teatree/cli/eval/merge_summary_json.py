"""``t3 eval merge-summary-json`` — combine per-shard eval-heal summary JSONs.

The full-suite CI heal run shards across a parallel matrix; each shard uploads its
own publish-safe per-scenario ``--summary-json``. This subcommand reads every
per-shard JSON (a directory or explicit paths) and writes ONE ``eval-heal-<sha>``
JSON with the same §2.4 schema — totals summed, scenarios concatenated — so the
``t3 eval ci-status`` download path reads the combined run unchanged. The shared
core lives in :mod:`teatree.eval.summary_json_merge`; only publish-safe rows are
read, so the merged artifact carries no transcript.

``--sha`` and ``--generated-at`` are PASSED IN (never computed here), mirroring
``t3 eval merge-summaries``, so the merge is deterministic. Writes to ``--out``
when given, else stdout.
"""

from pathlib import Path

import typer

from teatree.eval.summary_json_merge import merge_summary_json as _merge_summary_json


def merge_summary_json(
    inputs: list[str] = typer.Argument(..., help="Per-shard summary .json files, or a directory of them."),
    sha: str = typer.Option(..., "--sha", help="The commit SHA the run measured (injected)."),
    generated_at: str = typer.Option(..., "--generated-at", help="ISO-8601 timestamp (injected; never computed here)."),
    out: Path | None = typer.Option(None, "--out", help="Write the merged JSON to this path instead of stdout."),
) -> None:
    """Merge per-shard eval-heal summary JSONs into one §2.4 JSON (to --out or stdout)."""
    merged = _merge_summary_json(inputs, head_sha=sha, generated_at=generated_at)
    if out is not None:
        out.write_text(merged, encoding="utf-8")
    else:
        typer.echo(merged)
