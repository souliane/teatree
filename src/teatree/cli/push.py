"""``t3 push`` — the supported push path from the worker container (#3927)."""

import json

import typer

from teatree.core.forge_push import PushOutcome, push_branch


def push(
    repo: str = typer.Option(".", "--repo", help="Repository to push (defaults to the current directory)."),
    remote: str = typer.Option("origin", "--remote", help="Remote to push to."),
    branch: str = typer.Option("", "--branch", help="Branch to push (defaults to the checked-out branch)."),
    *,
    force_with_lease: bool = typer.Option(
        False, "--force-with-lease", help="Overwrite the remote branch only if it is where we last saw it."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the outcome as JSON."),
) -> None:
    """Push a branch using the forge credential the loop already holds.

    Resolves the token from GH_TOKEN, then TEATREE_GH_TOKEN, then the active
    overlay's pass store, and hands it to git as env only — never on argv and
    never written into the remote URL. Interactive credential prompts are
    disabled, so a missing credential fails immediately instead of hanging.
    The pre-push hooks still run.
    """
    outcome = push_branch(repo=repo, remote=remote, branch=branch, force_with_lease=force_with_lease)
    if json_output:
        typer.echo(json.dumps(outcome.as_dict()))
    else:
        _echo_outcome(outcome)
    if not outcome.ok:
        raise typer.Exit(code=1)


def _echo_outcome(outcome: PushOutcome) -> None:
    if not outcome.ok:
        typer.echo(f"push REFUSED on '{outcome.branch}' → {outcome.remote}")
        typer.echo(f"  {outcome.detail}")
        return
    typer.echo(f"pushed '{outcome.branch}' → {outcome.remote} (credential: {outcome.credential_source.value})")
    typer.echo(f"  {outcome.remote}/{outcome.branch} is now {outcome.pushed_sha}")
