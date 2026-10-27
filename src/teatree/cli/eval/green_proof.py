"""CLI gate for a merged eval summary against the catalog at the checked out SHA."""

import json
import os
from pathlib import Path

import typer

from teatree.eval.discovery import discover_catalog
from teatree.eval.green_proof import evaluate_green_proof
from teatree.eval.skip_guard import MEASURED_NOTHING_EXIT_CODE
from teatree.eval.summary_json import scenario_version


class DegradedCatalogError(RuntimeError):
    """The catalog is short a surface, so any count measured against it is short too."""


def expected_catalog_versions() -> dict[str, str]:
    """``name -> version`` for every scenario this checkout defines, or raise when the catalog is degraded."""
    catalog = discover_catalog()
    if not catalog.is_complete:
        raise DegradedCatalogError("; ".join(f"{name}: {reason}" for name, reason in sorted(catalog.degraded.items())))
    return {spec.name: scenario_version(spec) for spec in catalog.specs}


def green_proof(
    summary_json: Path = typer.Argument(..., help="The merged eval-heal-<sha> §2.4 summary JSON to prove green."),
    sha: str | None = typer.Option(None, "--sha", help="Exact checked-out commit SHA being proved."),
    incomplete_exit_code: int = typer.Option(
        1, "--incomplete-exit-code", help="Exit code for incomplete catalog coverage."
    ),
) -> None:
    """Assert the merged eval-heal JSON proves a full-suite green (whole catalog, 0 reds)."""
    if not summary_json.is_file():
        typer.echo(f"NOT A GREEN PROOF: no merged eval-heal JSON at {summary_json}")
        raise typer.Exit(1)
    try:
        expected = expected_catalog_versions()
    except DegradedCatalogError as exc:
        typer.echo(
            "NOT A GREEN PROOF: the scenario catalog is DEGRADED, so the count this proof would be "
            f"measured against is itself short — {exc}"
        )
        raise typer.Exit(1) from exc
    payload = json.loads(summary_json.read_text(encoding="utf-8"))
    proof = evaluate_green_proof(
        payload if isinstance(payload, dict) else {},
        expected=expected,
        expected_sha=sha or os.environ.get("CI_COMMIT_SHA") or os.environ.get("GITHUB_SHA", ""),
    )
    typer.echo(proof.summary)
    if proof.reds:
        raise typer.Exit(1)
    if proof.unmeasured or (isinstance(payload, dict) and payload.get("parked") is True):
        typer.echo("PARKED: the usage limit cut the run short; not a green proof, and nothing measured failed")
        raise typer.Exit(MEASURED_NOTHING_EXIT_CODE)
    if proof.incomplete:
        typer.echo("coverage incomplete: one or more scenarios did not finish grading")
        raise typer.Exit(incomplete_exit_code)
    if not proof.covers_the_catalog:
        typer.echo("coverage incomplete: exact catalog proof did not execute")
        raise typer.Exit(incomplete_exit_code)
