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

    Success means the remote was read back with `git ls-remote` and holds the
    branch at the local tip. Each way that fails exits with its own code, so a
    caller can branch on the fix it needs: 1 transport, 2 repo config, 3
    credential, 4 pre-push gate refused, 5 non-fast-forward, 6 not delivered,
    7 unverifiable.
    """
    outcome = push_branch(repo=repo, remote=remote, branch=branch, force_with_lease=force_with_lease)
    if json_output:
        typer.echo(json.dumps(outcome.as_dict()))
    else:
        _echo_outcome(outcome)
    if not outcome.ok:
        raise typer.Exit(code=outcome.exit_code)


def _echo_outcome(outcome: PushOutcome) -> None:
    if not outcome.ok:
        typer.echo(
            f"push REFUSED on '{outcome.branch}' → {outcome.remote} [{outcome.failure.value}, exit {outcome.exit_code}]"
        )
        typer.echo(f"  {outcome.detail}")
        return
    typer.echo(f"pushed '{outcome.branch}' → {outcome.remote} (credential: {outcome.credential_source.value})")
    typer.echo(f"  verified on the remote: {outcome.remote} refs/heads/{outcome.branch} = {outcome.pushed_sha}")
