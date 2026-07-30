"""``t3 fast-push`` — leak-gated sub-minute ship for session hand-offs (directive #8)."""

import json
from dataclasses import asdict
from pathlib import Path

import typer

from teatree.core.fast_push import FastPusher, FastPushOutcome


def fast_push(
    message: str = typer.Option("", "--message", "-m", help="Commit message (auto-generated when omitted)."),
    remaining: str = typer.Option("", "--remaining", help="Unfinished work, recorded as a REMAINING: PR-body section."),
    repo: str = typer.Option(".", "--repo", help="Repository to push (defaults to the current directory)."),
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the outcome as JSON."),
) -> None:
    """Stage, commit, push, and create-or-update the PR in one leak-gated step.

    Runs ONLY the leak gates (banned-terms, secret-scan, overlay-leak) —
    in-process, fail-closed — and skips every other hook/gate. Any leak
    finding refuses the push and prints the offending path/term.
    """
    outcome = FastPusher(repo=Path(repo).resolve(), message=message, remaining=remaining).run()
    if json_output:
        typer.echo(json.dumps(asdict(outcome)))
    else:
        _echo_outcome(outcome)
    if not outcome.ok:
        raise typer.Exit(code=1)


def _echo_outcome(outcome: FastPushOutcome) -> None:
    if not outcome.ok:
        typer.echo("fast-push REFUSED — leak gate findings (nothing committed, nothing pushed):")
        for finding in outcome.findings:
            location = finding.path or "-"
            typer.echo(f"  [{finding.gate}] {location}: {finding.detail}")
        return
    typer.echo(f"fast-push OK on '{outcome.branch}' (gates: {', '.join(outcome.executed_gates)})")
    typer.echo(f"  committed: {outcome.committed}  pushed: {outcome.pushed}")
    if outcome.pr_url:
        typer.echo(f"  PR {outcome.pr_action}: {outcome.pr_url}")
    elif outcome.pr_action == "skipped":
        typer.echo("  PR skipped: no gh/glab forge detected for the origin remote")
