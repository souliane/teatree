"""Finalize-time logic for the ``workspace finalize`` command.

The finalize partition of :mod:`teatree.core.management.commands.workspace`.
Holds the squash-then-rebase engine (:func:`run_finalize`) and the
defense-in-depth refusal that keeps a finalize soft-reset + commit off a managed
main clone's default branch (#752).
"""

from collections.abc import Callable
from pathlib import Path

from teatree.core.models import Ticket, Worktree
from teatree.paths import running_from_worktree
from teatree.utils import git
from teatree.utils.run import CommandFailedError


def refuse_finalize_on_main_clone_default(repo_dir: str, default_br: str) -> None:
    """Refuse to finalize-commit on a managed main clone's default branch (#752).

    Defense-in-depth guard: when a ``Worktree`` row's path resolves to a managed
    main clone (``.git`` is a *directory*, not a linked worktree's pointer file)
    AND it is checked out on the default branch, a finalize soft-reset + commit
    would land on the shared main clone's default branch. After the PR
    squash-merges on the remote, the rewritten SHA leaves the local default
    branch unable to fast-forward — the main-clone divergence that bricks
    ``t3 update``. Refuse before committing rather than producing it.

    A linked worktree (``.git`` is a pointer file) is never refused, and a main
    clone on a *feature* branch is left alone — only the main-clone + default-
    branch combination is the divergence hazard. When the branch cannot be
    resolved (a non-repo path yields an empty ``current_branch``) the guard
    does not match the default branch, so it does not refuse.
    """
    if running_from_worktree(Path(repo_dir)):
        return
    if git.current_branch(repo_dir) != default_br:
        return
    msg = (
        f"Refusing to finalize-commit on a managed main clone's default branch "
        f"({default_br}) at {repo_dir} — use a worktree; see worktree-first.\n"
        "Create a worktree first: t3 <overlay> workspace ticket <issue_url>"
    )
    raise SystemExit(msg)


def _squash_message(repo_dir: str, range_spec: str, count: int, requested: str) -> str:
    """The message a squash commit carries: the caller's, else the branch's own subject.

    Sourced from ``first_commit_message`` — the subject alone — because
    ``log --oneline`` is a DISPLAY format whose leading token is the abbreviated
    sha of the commit being squashed away, and that sha ended up in the message.
    """
    if requested:
        return requested
    subject, _ = git.first_commit_message(repo_dir, range_spec)
    return subject or f"Squash {count} commits"


def _finalize_one(worktree: Worktree, *, message: str, write: Callable[[str], None]) -> tuple[list[str], bool]:
    """Squash + rebase ONE worktree; return its report lines and whether it failed."""
    repo = worktree.repo_path
    repo_dir = (worktree.extra or {}).get("worktree_path") or repo
    default_br = git.default_branch(repo)
    results: list[str] = []
    try:
        status = git.status_porcelain(repo_dir)
        if status:
            return [f"{repo}: SKIPPED — uncommitted changes:\n{status}"], False

        refuse_finalize_on_main_clone_default(repo_dir, default_br)
        git.fetch(repo_dir, "origin", default_br)

        base = git.merge_base(repo_dir, f"origin/{default_br}")
        range_spec = f"{base}..HEAD"
        count = git.rev_count(repo_dir, range_spec)
        log = git.log_oneline(repo_dir, range_spec)
        if log:
            write(f"  {repo} commits ({count}):\n    " + "\n    ".join(log.splitlines()))

        if count > 1:
            git.soft_reset(repo_dir, base)
            git.commit(repo_dir, _squash_message(repo_dir, range_spec, count, message))
            results.append(f"{repo}: squashed {count} commits")
        else:
            results.append(f"{repo}: single commit, no squash needed")
    except CommandFailedError as exc:
        return [f"{repo}: finalize failed before the rebase — {exc}"], True

    try:
        git.rebase(repo_dir, f"origin/{default_br}")
    except CommandFailedError as exc:
        results.extend(
            [
                f"{repo}: rebase failed — {exc}",
                f"  To abort: git -C {repo_dir} rebase --abort",
                f"  To resolve: fix conflicts, git add, then: git -C {repo_dir} rebase --continue",
            ]
        )
        return results, True
    results.append(f"{repo}: rebased on {default_br}")
    return results, False


def run_finalize(ticket: Ticket, *, message: str, write: Callable[[str], None]) -> str:
    """Squash each worktree's commits into one, then rebase on the default branch.

    The engine for ``workspace finalize``: per worktree, skip on a dirty tree,
    refuse a main-clone default-branch finalize, squash >1 commit into ``message``
    (or the branch's own first subject), then rebase on ``origin/<default>``. A
    failure never aborts the other worktrees, but the command exits 1 once every
    worktree has been attempted — a swallowed git failure reported finalize as
    complete over a tree that was never squashed or rebased.
    """
    results: list[str] = []
    failed = False
    for worktree in Worktree.objects.for_ticket(ticket):
        lines, worktree_failed = _finalize_one(worktree, message=message, write=write)
        results.extend(lines)
        failed = failed or worktree_failed
    report = "\n".join(results)
    if failed:
        write(report)
        raise SystemExit(1)
    return report
