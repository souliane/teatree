"""``t3 eval green-proof`` — assert a merged eval-heal JSON is the full-suite green proof (#3202).

The CI heal workflow's combine job folds every shard into ONE ``eval-heal-<sha>``
§2.4 JSON; this subcommand reads it and asserts it PROVES a full-suite green: the
run executed scenarios (``total > 0``) and recorded ZERO reds (no behavioral,
``infra_*``, ``judge``, or ``no_coverage`` scenario). Exits non-zero on any red or
an empty run, so the merged JSON becomes an enforced CI gate — that JSON is the
proof. The verdict logic lives in :mod:`teatree.eval.green_proof`; this is a thin
JSON-read shell.
"""

import json
from pathlib import Path

import typer

from teatree.eval.green_proof import evaluate_green_proof


def green_proof(
    summary_json: Path = typer.Argument(..., help="The merged eval-heal-<sha> §2.4 summary JSON to prove green."),
) -> None:
    """Assert the merged eval-heal JSON proves a full-suite green (executed, 0 reds)."""
    if not summary_json.is_file():
        typer.echo(f"NOT A GREEN PROOF: no merged eval-heal JSON at {summary_json}")
        raise typer.Exit(1)
    payload = json.loads(summary_json.read_text(encoding="utf-8"))
    proof = evaluate_green_proof(payload if isinstance(payload, dict) else {})
    typer.echo(proof.summary)
    if not proof.is_green:
        raise typer.Exit(1)
