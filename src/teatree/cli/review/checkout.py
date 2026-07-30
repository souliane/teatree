"""``t3 review checkout <PR_URL> --sha <head>`` — the verify-or-fail cold-review checkout.

The CLI seam over :func:`teatree.utils.review_checkout.add_review_worktree_at_head`
(#2132). The helper shipped with tests and a skill reference but no runnable
entry point, so the only way to reach it was a hand-written ``python -c`` — a
workaround the repo's "fix the CLI, never work around it" rule forbids, and one
no reviewer took. What reviewers reached for instead was the raw ``git worktree
add <branch>`` the helper exists to replace, which silently falls back to a
stale tree when the branch is already checked out elsewhere.

The head ref is derived from the URL's forge (:func:`head_ref_for`), so the
caller passes the PR/MR URL it already has rather than resolving a branch name
through a forge API first.

Exit codes mirror the sibling ``t3 review run``:

* ``0`` — the worktree materialised at the expected SHA; its path is on stdout.
* ``1`` — git was invoked and refused (``checkout_failed``), or the
    materialised HEAD diverged from ``--sha`` (``stale_checkout``).
* ``2`` — the URL was refused before any git call (``bad_url``).
"""

import json

import typer

from teatree.cli.review.service import review_app
from teatree.url_classify import Forge, forge_of, repo_and_iid
from teatree.utils.review_checkout import StaleReviewCheckoutError, add_review_worktree_at_head
from teatree.utils.run import CommandFailedError


def head_ref_for(url: str) -> str:
    """The forge's read-only ref naming the PR/MR head, or ``""`` if unparsable.

    Both forges publish the head under a fetchable pseudo-ref, so no API call
    (and no token) is needed to name the commit under review.
    """
    parsed = repo_and_iid(url)
    if parsed is None:
        return ""
    _, number = parsed
    forge = forge_of(url)
    if forge is Forge.GITHUB:
        return f"refs/pull/{number}/head"
    if forge is Forge.GITLAB:
        return f"refs/merge-requests/{number}/head"
    return ""


@review_app.command(name="checkout")
def checkout(
    url: str = typer.Argument(help="PR/MR URL whose head to materialise."),
    sha: str = typer.Option(..., "--sha", help="Full 40-char head SHA the checkout must land on."),
    repo: str = typer.Option(".", "--repo", help="Local clone to add the review worktree from."),
    remote: str = typer.Option("origin", "--remote", help="Remote to fetch the head ref from."),
    base_dir: str = typer.Option("", "--base-dir", help="Parent directory for the temp worktree."),
) -> None:
    """Materialise a detached review worktree at the exact reviewed head.

    Prints ``{"worktree": ..., "ref": ..., "sha": ..., "url": ...}`` on success.
    A HEAD that does not equal ``--sha`` is a hard failure, never a fallback to
    whatever tree happened to be reachable — the review runs on the pushed head
    or not at all. Remove the worktree with ``git worktree remove`` when done.
    """
    ref = head_ref_for(url)
    if not ref:
        typer.echo(json.dumps({"error": "bad_url", "url": url}, sort_keys=True))
        raise typer.Exit(code=2)
    try:
        worktree = add_review_worktree_at_head(
            repo,
            ref=ref,
            expected_sha=sha,
            remote=remote,
            base_dir=base_dir or None,
        )
    except StaleReviewCheckoutError as exc:
        typer.echo(json.dumps({"error": "stale_checkout", "url": url, "detail": str(exc)}, sort_keys=True))
        raise typer.Exit(code=1) from None
    except CommandFailedError as exc:
        typer.echo(json.dumps({"error": "checkout_failed", "url": url, "detail": str(exc)}, sort_keys=True))
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps({"ref": ref, "sha": sha, "url": url, "worktree": worktree}, sort_keys=True))
