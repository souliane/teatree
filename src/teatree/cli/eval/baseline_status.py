"""``t3 eval baseline-status`` — is a recent full-catalog green proof on record?

A one-shard smoke run stays green without saying anything about the rest of the catalog, so
this reads the newest completed full-suite ``eval-ci-heal`` runs on a branch, downloads each
``eval-heal-<sha>`` summary and judges it with :func:`teatree.eval.green_proof.evaluate_baseline`
against the catalog as this checkout defines it. Exit 0 only for a recent green proof.
"""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer

from teatree.backends.github.ci_eval_client import (
    DEFAULT_CI_EVAL_REPO,
    EVAL_CI_HEAL_WORKFLOW,
    GhCiEvalClient,
    build_ci_eval_client,
)
from teatree.cli.eval.green_proof import DegradedCatalogError, expected_catalog_versions
from teatree.eval.green_proof import BASELINE_MAX_AGE, evaluate_baseline
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError

#: Newest successful runs read before giving up, so a run of parked or unreadable ones is bounded.
_RUNS_TO_READ = 3


def _newest_payloads(client: GhCiEvalClient, *, ref: str) -> tuple[list[RawAPIDict], list[str]]:
    """The merged summaries of the newest successful runs, newest first, plus a note per unreadable one."""
    runs = [
        run
        for run in client.list_runs(EVAL_CI_HEAL_WORKFLOW, branch=ref)
        if run.get("status") == "completed" and run.get("conclusion") == "success"
    ]
    payloads: list[RawAPIDict] = []
    notes: list[str] = []
    for run in runs[:_RUNS_TO_READ]:
        run_id, name = run.get("databaseId"), f"eval-heal-{run.get('headSha')}"
        with tempfile.TemporaryDirectory() as scratch:
            try:
                client.download_artifact(str(run_id), name=name, dest_dir=Path(scratch))
            except (CommandFailedError, FileNotFoundError) as exc:
                notes.append(f"run {run_id}: could not download {name}: {exc}")
                continue
            files = sorted(Path(scratch).rglob("*.json"))
            payload = json.loads(files[0].read_text(encoding="utf-8")) if files else None
        if isinstance(payload, dict):
            payloads.append(payload)
        else:
            notes.append(f"run {run_id}: {name} carried no summary JSON")
    return payloads, notes


def baseline_status(
    ref: str = typer.Option("main", "--ref", help="Branch whose eval-ci-heal full-suite runs count as the baseline."),
    max_age_days: int = typer.Option(
        BASELINE_MAX_AGE.days, "--max-age-days", min=1, help="Oldest full-suite proof still called current."
    ),
    repo: str = typer.Option(DEFAULT_CI_EVAL_REPO, "--repo", help="owner/repo the eval-ci-heal workflow lives in."),
) -> None:
    """Report whether a recent full-suite green proof over the current catalog is on record."""
    try:
        expected = expected_catalog_versions()
    except DegradedCatalogError as exc:
        typer.echo(f"BASELINE UNKNOWN: the scenario catalog is DEGRADED, so there is no full count to measure — {exc}")
        raise typer.Exit(1) from exc
    try:
        payloads, notes = _newest_payloads(build_ci_eval_client(repo), ref=ref)
    except CommandFailedError as exc:
        typer.echo(f"BASELINE UNKNOWN: could not list {EVAL_CI_HEAL_WORKFLOW} runs on {ref}: {exc}")
        raise typer.Exit(1) from exc
    status = evaluate_baseline(payloads, expected=expected, now=datetime.now(UTC), max_age=timedelta(days=max_age_days))
    for note in notes:
        typer.echo(f"  note: {note}")
    typer.echo(status.summary)
    if not status.is_green:
        raise typer.Exit(1)
