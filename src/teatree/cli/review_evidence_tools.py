"""Measure and persist deterministic review evidence for one committed head."""

from pathlib import Path
from threading import Event, Thread

import typer


def review_evidence_command(
    expect_sha: str = typer.Option(..., "--expect-sha", help="Full committed head SHA to measure."),
    base: str = typer.Option("origin/main", "--base", help="Target branch ref for the changed set."),
) -> None:
    from teatree.quality.review_evidence import (  # noqa: PLC0415 — measure only on invocation
        measure_review_evidence,
        write_review_evidence,
    )

    repo = Path.cwd()
    finished = Event()

    def heartbeat() -> None:
        while not finished.wait(30):
            typer.echo(f"review evidence: still measuring {expect_sha[:12]}")

    monitor = Thread(target=heartbeat, daemon=True)
    monitor.start()
    try:
        evidence = measure_review_evidence(repo, base_ref=base, expect_sha=expect_sha)
        write_review_evidence(repo, evidence)
    finally:
        finished.set()
        monitor.join(timeout=1)
    typer.echo(f"review evidence measured {evidence.head_sha}: {len(evidence.matrix.rows)} changed test(s)")
    typer.echo(evidence.render())


def register(app: typer.Typer) -> None:
    app.command("review-evidence")(review_evidence_command)
