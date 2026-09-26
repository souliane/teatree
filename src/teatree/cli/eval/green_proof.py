"""CLI gate for a merged eval summary against the catalog at the checked out SHA."""

import json
import os
from pathlib import Path

import typer

from teatree.eval.discovery import discover_catalog
from teatree.eval.green_proof import evaluate_green_proof
from teatree.eval.summary_json import scenario_version


def green_proof(
    summary_json: Path = typer.Argument(..., help="The merged eval-heal-<sha> §2.4 summary JSON to prove green."),
    sha: str | None = typer.Option(None, "--sha", help="Exact checked-out commit SHA being proved."),
) -> None:
    """Assert the merged eval-heal JSON proves a full-suite green (whole catalog, 0 reds)."""
    if not summary_json.is_file():
        typer.echo(f"NOT A GREEN PROOF: no merged eval-heal JSON at {summary_json}")
        raise typer.Exit(1)
    catalog = discover_catalog()
    if not catalog.is_complete:
        reasons = "; ".join(f"{name}: {reason}" for name, reason in sorted(catalog.degraded.items()))
        typer.echo(
            "NOT A GREEN PROOF: the scenario catalog is DEGRADED, so the count this proof would be "
            f"measured against is itself short — {reasons}"
        )
        raise typer.Exit(1)
    payload = json.loads(summary_json.read_text(encoding="utf-8"))
    expected = {spec.name: scenario_version(spec) for spec in catalog.specs}
    proof = evaluate_green_proof(
        payload if isinstance(payload, dict) else {},
        expected=expected,
        expected_sha=sha or os.environ.get("CI_COMMIT_SHA") or os.environ.get("GITHUB_SHA", ""),
    )
    typer.echo(proof.summary)
    if not proof.is_green:
        raise typer.Exit(1)
