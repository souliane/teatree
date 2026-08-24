"""``t3 eval green-proof`` — assert a merged eval-heal JSON is the full-suite green proof (#3202).

The CI heal workflow's combine job folds every shard into ONE ``eval-heal-<sha>``
§2.4 JSON; this subcommand reads it and asserts it PROVES a full-suite green: the
run covered the whole catalog and recorded ZERO reds (no behavioral, ``infra_*``,
``judge``, or ``no_coverage`` scenario). Exits non-zero on any red or a short run,
so the merged JSON becomes an enforced CI gate — that JSON is the proof.

The expected scenario count is the live catalog at the checked-out sha
(:func:`teatree.eval.discovery.discover_catalog`) — the combine job checks out the
eval'd ref, so the two agree by construction. Reading it here rather than taking
a CI-supplied number keeps the count from drifting out of the workflow's step
name, which is where "231/231" was previously asserted. The verdict logic lives
in :mod:`teatree.eval.green_proof`; this is a thin JSON-read shell.

A DEGRADED catalog is refused before the count is taken. An overlay that raised — or
that succeeded while naming a directory which is not there — shrinks the catalog and
the expected count together, so the run "covers" a denominator derived from the same
incomplete read: a completeness gate that cannot see its own incompleteness. The
``catalog-discovery`` lane catches it under bare ``t3 eval``, which the combine job
does not run, and additionally floors the core count so a shrink there costs an edit.

An ``advisory`` (``surface: interactive``) row is PRINTED under the headline but
never withholds the proof — it is one of the verdicts named in
:data:`teatree.eval.surface.ADVISORY_EXEMPT_VERDICT_POINTS`, so the CI log still
shows an interactive regression that this gate deliberately does not fail on.
"""

import json
from pathlib import Path

import typer

from teatree.eval.discovery import discover_catalog
from teatree.eval.green_proof import evaluate_green_proof


def green_proof(
    summary_json: Path = typer.Argument(..., help="The merged eval-heal-<sha> §2.4 summary JSON to prove green."),
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
    proof = evaluate_green_proof(payload if isinstance(payload, dict) else {}, expected_total=len(catalog.specs))
    typer.echo(proof.summary)
    if not proof.is_green:
        raise typer.Exit(1)
