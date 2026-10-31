"""Revert each hunk of in-scope production code and record which hunks no test notices."""

import shlex
from pathlib import Path

import typer

from teatree.quality.revert_sweep import RevertSweepError, measure_revert_sweep, write_revert_sweep


def revert_sweep_command(
    expect_sha: str = typer.Option(..., "--expect-sha", help="Full committed head SHA to sweep."),
    base: str = typer.Option("origin/main", "--base", help="Target branch ref for the changed set."),
    test_command: str = typer.Option(
        "", "--test-command", help="Pytest command to run per hunk (default: repo runner)."
    ),
) -> None:
    repo = Path.cwd()
    try:
        sweep = measure_revert_sweep(
            repo,
            base_ref=base,
            expect_sha=expect_sha,
            test_command=shlex.split(test_command) or None,
        )
        write_revert_sweep(repo, sweep)
    except RevertSweepError as exc:
        typer.echo(f"revert sweep failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(sweep.render())
    if not sweep.clean:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("revert-sweep", help="Revert each hunk of in-scope code and record the hunks no test pins.")(
        revert_sweep_command
    )
