"""Reap orphaned RAW git worktrees that no teatree ``Worktree`` row tracks (#2361).

Sub-agents and coders create their own worktrees with a bare ``git worktree
add`` — no ``Worktree`` DB row. ``clean-all``'s row-driven reaper never iterates
them, so they accumulate indefinitely (a real host reached 183 of them). This
module is the gap-closing pass: discover every git worktree under the
workspace's main clones, subtract the DB-tracked set, and dispose of the
remainder under a safety-first policy.

Disposition per orphan (the #706 data-loss contract is never bypassed). A
*merged / gone / no-unique-work* orphan — whose commits are already on
``origin/<default>``, or a detached worktree with nothing reachable only from it
— is recoverable from the default branch, so it is removed and pruned. A
*unique-unpushed-work* orphan carries commits absent from every remote: it is
KEPT with a warning (salvage it by pushing the branch — the snapshot-then-reap
path is gone; potentially-needed work is never destroyed). An
*uncommitted-changes* orphan (a live worktree an agent may be mid-task in) is
always KEPT — a clean removal would lose the dirty diff.
"""

import logging
from pathlib import Path

from teatree.core.branch_classification import is_squash_merged
from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.clone_paths import resolve_clone_path
from teatree.core.models import Worktree
from teatree.core.worktree_paths import paths_match
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)


def _raw_worktree_paths(repo: str) -> dict[str, str]:
    """Return ``{worktree_path: branch}`` for every LINKED worktree of ``repo``.

    Parses ``git worktree list --porcelain``. The main checkout (the record whose
    path is ``repo`` itself) is excluded — only the linked worktrees are
    candidates. A detached worktree carries no ``branch`` line; it is recorded
    with the literal ``HEAD`` (:data:`git.DETACHED_HEAD`).
    """
    raw = git.run(repo=repo, args=["worktree", "list", "--porcelain"])
    main = str(Path(repo).resolve())
    result: dict[str, str] = {}
    current_path = ""
    current_branch = ""
    for line in [*raw.splitlines(), "worktree "]:  # trailing sentinel flushes the last record
        if line.startswith("worktree "):
            if current_path and str(Path(current_path).resolve()) != main:
                result[current_path] = current_branch or git.DETACHED_HEAD
            current_path = line.removeprefix("worktree ")
            current_branch = ""
        elif line.startswith("branch refs/heads/"):
            current_branch = line.removeprefix("branch refs/heads/")
    return result


def _db_tracked_paths() -> list[str]:
    """On-disk paths of every git worktree teatree has a ``Worktree`` row for.

    Returned unresolved so :func:`paths_match` can apply its full symlink-variant
    set per comparison (a bare ``.resolve()`` set misses the ``/private`` literal
    twin a ``git worktree list`` path may carry).
    """
    return [wt.worktree_path for wt in Worktree.objects.all() if wt.worktree_path]


def _candidate_clones(workspace: Path) -> set[str]:
    """The main clones whose worktree registries may hold orphaned worktrees.

    A worktree's registry lives in its source clone, so orphans are found by
    listing each known main clone's worktrees. The known clones are the
    ``clone_path`` of every ``Worktree`` row (where sub-agents branch from) plus
    the current working directory when it is itself a main clone (``.git`` is a
    directory, not the gitdir-pointer file a linked worktree carries).
    """
    clones: set[str] = set()
    for wt in Worktree.objects.all():
        clone = resolve_clone_path(workspace, wt)
        if clone is not None and (clone / ".git").is_dir():
            clones.add(str(clone.resolve()))
    cwd = Path.cwd()
    if (cwd / ".git").is_dir():
        clones.add(str(cwd.resolve()))
    return clones


def _branch_has_unique_work(repo: str, branch: str, wt_path: str) -> bool:
    """Whether ``branch`` carries unmerged work absent from every remote (data loss on removal).

    The #706 primitive (``commits_absent_from_all_remotes``) reports a commit as
    "absent" whenever its SHA is on no remote. A squash-merge rewrites the
    branch's commits into ONE new SHA on the default branch and the source branch
    is typically deleted on merge — the dominant teatree case — so the original
    commit is absent-from-all-remotes by SHA even though its WORK is shipped.
    Treating that as unique work wrongly keeps a resolved orphan. So a branch
    counts as unique unpushed work only when its commits are absent from every
    remote AND :func:`is_squash_merged` (patch-id ``git cherry``) does NOT find
    the work captured on ``origin/<default>``.

    A named branch is probed from the shared object store (``repo``); a detached
    HEAD is meaningful only in the worktree dir, so it is probed there. Fails
    CLOSED — an inconclusive absence probe (corrupt repo, unknown ref) reads as
    "has unique work" so the worktree is kept, never reaped on uncertainty.
    """
    probe_repo = wt_path if branch == git.DETACHED_HEAD else repo
    try:
        absent = bool(git.commits_absent_from_all_remotes(probe_repo, branch))
    except CommandFailedError:
        return True
    if not absent:
        return False
    return not (branch != git.DETACHED_HEAD and is_squash_merged(repo, branch, git.default_branch(repo)))


def _is_dirty(wt_path: str) -> bool:
    """Whether the worktree has uncommitted changes (a live, mid-task worktree)."""
    return bool(git.status_porcelain(wt_path))


def _remove_orphan(repo: str, wt_path: str, branch: str) -> bool:
    """Remove the orphan worktree and prune the registry. Returns success."""
    if not git.worktree_remove(repo, wt_path):
        return False
    git.run(repo=repo, args=["worktree", "prune"])
    if branch != git.DETACHED_HEAD:
        git.branch_delete(repo, branch)
    return True


def _reap_one_orphan(repo: str, wt_path: str, branch: str) -> str:
    """Dispose of one orphaned raw worktree under the keep-unproven-work policy."""
    label = f"{branch} ({wt_path})"
    if is_clean_ignored(branch):
        return f"SKIPPED orphan '{label}': matches clean_ignore — keeping"
    if _is_dirty(wt_path):
        return f"KEPT orphan '{label}': uncommitted changes — never reaped"
    if _branch_has_unique_work(repo, branch, wt_path):
        return f"KEPT orphan '{label}': unpushed work not on any remote — push it to salvage, never reaped"
    if _remove_orphan(repo, wt_path, branch):
        return f"Reaped orphan worktree (work already on remote): {label}"
    return f"SKIPPED orphan '{label}': git worktree remove failed"


def reap_orphan_raw_worktrees(workspace: Path) -> list[str]:
    """Discover and dispose of raw git worktrees no ``Worktree`` row tracks (#2361).

    For every main clone teatree knows about, every linked worktree whose
    absolute path is NOT in the DB-tracked set is an orphan. Each is classified
    and disposed of by :func:`_reap_one_orphan`: a merged/gone orphan is reaped; an
    orphan with unpushed work or uncommitted changes is KEPT (never destroyed).
    The pass is resilient: a clone whose worktree registry cannot be read (corrupt
    / origin-less) is skipped with a warning rather than aborting the run.
    """
    tracked = _db_tracked_paths()
    cleaned: list[str] = []
    for repo in sorted(_candidate_clones(workspace)):
        try:
            worktrees = _raw_worktree_paths(repo)
        except CommandFailedError as exc:
            cleaned.append(f"SKIPPED clone {repo}: could not list worktrees ({exc})")
            continue
        for wt_path, branch in sorted(worktrees.items()):
            if any(paths_match(wt_path, t) for t in tracked):
                continue
            cleaned.append(_reap_one_orphan(repo, wt_path, branch))
    return cleaned
