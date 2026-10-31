"""``t3 push`` — the supported push path from the worker container (#3927)."""

import json
from pathlib import Path

import typer

from teatree.core.forge_push import PushOutcome, push_branch
from teatree.core.forge_push_verdict import PUSH_EXIT_CODES, PushFailure
from teatree.core.invocation_cwd import operator_cwd
from teatree.core.push.pr_head_check import PrHeadStatus, PrHeadVerdict, confirm_pr_head


def push(
    repo: Path = typer.Option(operator_cwd, "--repo", help="Repository to push (default: where t3 was invoked)."),
    remote: str = typer.Option("origin", "--remote", help="Remote to push to."),
    branch: str = typer.Option(
        "",
        "--branch",
        help="Branch to push (default: the checked-out one). On a detached HEAD, HEAD:<branch> publishes HEAD as it.",
    ),
    *,
    force_with_lease: bool = typer.Option(
        False, "--force-with-lease", help="Overwrite the remote branch only if it is where we last saw it."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the outcome as JSON."),
) -> None:
    """Push a branch using the forge credential the loop already holds.

    Resolves the token from the repository's owning overlay and hands it to git
    as env only — never on argv and
    never written into the remote URL. Interactive credential prompts are
    disabled, so a missing credential fails immediately instead of hanging.
    The pre-push hooks still run.

    `--repo` defaults to the checkout t3 was invoked from, and a relative
    one resolves from there. `--branch HEAD:<branch>` pushes a detached
    HEAD to <branch> without creating a local branch; no other refspec
    form is accepted.

    Success means the remote was read back with `git ls-remote` and holds the
    branch at the local tip. Each way that fails exits with its own code, so a
    caller can branch on the fix it needs: 1 transport, 2 config, 3 credential,
    4 gate-refused, 5 non-fast-forward, 6 not-on-remote (and 6 for a
    remote-sha-mismatch), 7 unverifiable, 8 remote-rejected, 9 gate-aborted.

    After the remote read-back, the open PR's head is compared with the pushed
    sha: a PR head that lags exits 6 naming both shas, a head that cannot be
    read exits 7, and a branch with no open PR passes with a note.
    """
    repo_path = str(operator_cwd() / repo)
    outcome = push_branch(repo=repo_path, remote=remote, branch=branch, force_with_lease=force_with_lease)
    pr_head = confirm_pr_head(repo_path, outcome.branch, outcome.pushed_sha) if outcome.ok else None
    if json_output:
        typer.echo(json.dumps({**outcome.as_dict(), "pr_head": pr_head.as_dict()} if pr_head else outcome.as_dict()))
    else:
        _echo_outcome(outcome)
        if pr_head is not None:
            typer.echo(f"  PR head {pr_head.status.value}: {pr_head.detail}")
    if not outcome.ok:
        raise typer.Exit(code=outcome.exit_code)
    if pr_head is not None and not pr_head.ok:
        raise typer.Exit(code=_pr_head_exit_code(pr_head))


def _pr_head_exit_code(verdict: PrHeadVerdict) -> int:
    failure = PushFailure.REMOTE_SHA_MISMATCH if verdict.status is PrHeadStatus.LAGS else PushFailure.UNVERIFIABLE
    return PUSH_EXIT_CODES[failure]


def _echo_outcome(outcome: PushOutcome) -> None:
    if not outcome.ok:
        typer.echo(
            f"push REFUSED on '{outcome.branch}' → {outcome.remote} [{outcome.failure.value}, exit {outcome.exit_code}]"
        )
        typer.echo(f"  {outcome.detail}")
        return
    typer.echo(f"pushed '{outcome.branch}' → {outcome.remote} (credential: {outcome.credential_source.value})")
    typer.echo(f"  verified on the remote: {outcome.remote} refs/heads/{outcome.branch} = {outcome.pushed_sha}")
