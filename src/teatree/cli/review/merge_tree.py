"""``t3 review merge-tree`` — the one-step merge-result extract (#4251).

The runtime-probe sibling of ``t3 review checkout``: that command materialises
the BRANCH head (right for reading the diff), this one materialises the tree
the merge would produce (right for every runtime measurement). A finding taken
on the branch alone reports what ``main`` did to a file since the branch was
cut, so a cold reviewer measuring behaviour probes the extract this prints.

Exit codes:

* ``0`` — the merge result is extracted; its path (and the resolved ends) on stdout.
* ``1`` — ``base`` and ``head`` conflict, or git refused the invocation.
"""

import json
from dataclasses import asdict

import typer

from teatree.cli.review.service import review_app
from teatree.utils.merge_tree_extract import MergeTreeConflictError, extract_merge_result
from teatree.utils.run import CommandFailedError


@review_app.command(name="merge-tree")
def merge_tree(
    base: str = typer.Option("origin/main", "--base", help="Target-branch ref the PR would merge into."),
    head: str = typer.Option("HEAD", "--head", help="PR head ref to merge into --base."),
    repo: str = typer.Option(".", "--repo", help="Local clone holding both refs."),
    into: str = typer.Option("", "--into", help="Destination directory (default: a fresh temp dir)."),
    *,
    no_git: bool = typer.Option(
        False, "--no-git", help="Leave a bare directory instead of a primary checkout carrying the real origin."
    ),
) -> None:
    """Extract the merge result of --base and --head into a plain directory.

    Never a git worktree — resolve_data_dir auto-isolates one onto a per-worktree
    DB, which is its own wrong-answer generator. The extract is a primary checkout
    whose origin is the source clone's real remote URL, so code reading repository
    identity resolves it; --no-git opts out.
    """
    try:
        extract = extract_merge_result(repo, base=base, head=head, into=into, init_git=not no_git)
    except MergeTreeConflictError as exc:
        typer.echo(json.dumps({"error": "merge_conflict", "detail": str(exc)}, sort_keys=True))
        raise typer.Exit(code=1) from None
    except CommandFailedError as exc:
        typer.echo(json.dumps({"error": "merge_tree_failed", "detail": str(exc)}, sort_keys=True))
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(asdict(extract), sort_keys=True))
