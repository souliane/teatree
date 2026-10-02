"""Materialise a PR's MERGE RESULT as a plain checkout, in one step (#4251).

A cold reviewer that probes the branch checkout measures what ``main`` did to a
file since the branch was cut, not what the PR did — which produced a
high-severity HOLD on a docs-only PR about ``src/`` code it does not touch. The
merge result is the tree that answers the question, but producing one was a
four-command recipe (``merge-tree`` → ``write-tree`` → ``archive`` → extract),
and every step was a chance to skip it and probe the branch instead.

Three probe environments have each produced a confident wrong answer on this
repo, so the extract forecloses all three. A **git worktree** is never used —
:func:`teatree.paths.resolve_data_dir` auto-isolates one onto a per-worktree DB.
The extract is a **primary checkout** (``git init``), not a bare directory — a
tree with no ``.git`` breaks every test that shells out to git. And its
``origin`` is the **source clone's real remote URL**, not a local path —
``resolved_repo_slug`` returns ``""`` for an unresolvable origin, silently
defeating every repo-scoped match downstream.
"""

import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from teatree.utils.run import CommandFailedError, run_allowed_to_fail, run_checked

#: ``git merge-tree --write-tree`` exits 1 on a conflicted merge; anything above
#: that is git refusing the invocation itself (a bad ref, not a repo).
_CONFLICT_RC = 1


class MergeTreeConflictError(RuntimeError):
    """*base* and *head* do not merge cleanly, so there is no merge result to probe.

    Raised instead of extracting a partially-resolved tree: a probe run against
    a tree git never produced answers a question nobody asked.
    """


@dataclass(frozen=True, slots=True)
class MergeResultExtract:
    """Where the merge result was materialised, and which ends produced it."""

    path: str
    tree_oid: str
    base_sha: str
    head_sha: str


def extract_merge_result(
    repo: str,
    *,
    base: str = "origin/main",
    head: str = "HEAD",
    into: str = "",
    init_git: bool = True,
) -> MergeResultExtract:
    """Extract the *base*+*head* merge result into a plain directory and describe it.

    *into* defaults to a fresh temp dir, which is deliberately outside every
    checkout: the worktree probe walks UP from the tree looking for a ``.git``
    entry, so an extract nested inside a worktree would inherit that worktree's
    per-worktree DB isolation. Raises :class:`MergeTreeConflictError` when the
    two ends conflict, leaving nothing behind.
    """
    tree_oid = _merged_tree_oid(repo, base=base, head=head)
    destination = Path(into) if into else Path(tempfile.mkdtemp(prefix="t3-merge-tree-"))
    destination.mkdir(parents=True, exist_ok=True)
    _extract_tree(repo, tree_oid=tree_oid, destination=destination)
    if init_git:
        _init_with_source_origin(repo, destination=destination)
    return MergeResultExtract(
        path=str(destination),
        tree_oid=tree_oid,
        base_sha=_rev_parse(repo, base),
        head_sha=_rev_parse(repo, head),
    )


def _merged_tree_oid(repo: str, *, base: str, head: str) -> str:
    result = run_allowed_to_fail(
        ["git", "-C", repo, "merge-tree", "--write-tree", base, head],
        expected_codes=(0, _CONFLICT_RC),
    )
    if result.returncode == _CONFLICT_RC:
        message = f"{base} and {head} do not merge cleanly — no merge result to probe:\n{result.stdout.strip()}"
        raise MergeTreeConflictError(message)
    return result.stdout.strip().splitlines()[0].strip()


def _extract_tree(repo: str, *, tree_oid: str, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="t3-merge-tar-") as staging:
        archive = Path(staging) / "tree.tar"
        run_checked(["git", "-C", repo, "archive", "--format=tar", "--output", str(archive), tree_oid])
        with tarfile.open(archive) as tar:
            tar.extractall(destination, filter="data")


def _init_with_source_origin(repo: str, *, destination: Path) -> None:
    """Make the extract a primary checkout carrying the SOURCE clone's real remote URL."""
    origin = _source_origin_url(repo)
    run_checked(["git", "-C", str(destination), "init", "-q", "-b", "merge-result"])
    if origin:
        run_checked(["git", "-C", str(destination), "remote", "add", "origin", origin])


def _source_origin_url(repo: str) -> str:
    try:
        return run_checked(["git", "-C", repo, "remote", "get-url", "origin"]).stdout.strip()
    except CommandFailedError:
        return ""


def _rev_parse(repo: str, ref: str) -> str:
    return run_checked(["git", "-C", repo, "rev-parse", ref]).stdout.strip()
