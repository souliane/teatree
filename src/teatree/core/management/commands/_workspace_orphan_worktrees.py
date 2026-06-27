"""Reap orphaned RAW git worktrees that no teatree ``Worktree`` row tracks (#2361).

Sub-agents and coders create their own worktrees with a bare ``git worktree
add`` — no ``Worktree`` DB row. ``clean-all``'s row-driven reaper never iterates
them, so they accumulate indefinitely (a real host reached 183 of them). This
module is the gap-closing pass: discover every git worktree under the
workspace's main clones, subtract the DB-tracked set, and dispose of the
remainder under a safety-first policy.

Disposition per orphan (the #706/#835 data-loss contract is never bypassed).
A *merged / gone / no-unique-work* orphan — whose commits are already on
``origin/<default>``, or a detached worktree with nothing reachable only from it
— is recoverable from the default branch, so it is removed and pruned. A
*unique-unpushed-work* orphan carries commits absent from every remote: under
``reap_unsynced='keep'`` (the default) it is KEPT with a warning; under
``reap_unsynced='snapshot'`` a recovery artifact (the shared
:func:`~teatree.core.worktree_snapshot.capture_worktree_snapshot` bundle+diff)
is captured FIRST, verified to have materialised, and only THEN is the worktree
removed — so the commits are always recoverable, and a snapshot that fails to
materialise keeps the worktree (the #706 guard). An *uncommitted-changes* orphan
(a live worktree an agent may be mid-task in) is always KEPT regardless of
policy — a clean removal would lose the dirty diff.
"""

import logging
from pathlib import Path
from typing import Literal

from teatree.core.clone_paths import resolve_clone_path
from teatree.core.management.commands._workspace_cleanup import is_clean_ignored, is_squash_merged
from teatree.core.models import Worktree
from teatree.core.worktree_paths import paths_match
from teatree.core.worktree_snapshot import capture_worktree_snapshot
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

ReapUnsyncedPolicy = Literal["keep", "snapshot"]


def _raw_worktree_paths(repo: str) -> dict[str, str]:
    """Return ``{worktree_path: branch}`` for every LINKED worktree of ``repo``.

    Parses ``git worktree list --porcelain``. The main checkout (the record whose
    path is ``repo`` itself) is excluded — only the linked worktrees are
    candidates. A detached worktree carries no ``branch`` line; it is recorded
    with the literal ``HEAD`` (:data:`git.DETACHED_HEAD`) so the snapshot path can
    bundle the detached commits from the worktree dir.
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


def _reap_one_orphan(
    repo: str,
    wt_path: str,
    branch: str,
    *,
    reap_unsynced: ReapUnsyncedPolicy,
) -> str:
    """Dispose of one orphaned raw worktree per the safety policy. Returns a result line."""
    label = f"{branch} ({wt_path})"
    if is_clean_ignored(branch):
        return f"SKIPPED orphan '{label}': matches clean_ignore — keeping"
    if _is_dirty(wt_path):
        return f"KEPT orphan '{label}': uncommitted changes — never reaped without an explicit snapshot"
    if not _branch_has_unique_work(repo, branch, wt_path):
        return _remove_recoverable_orphan(repo, wt_path, branch, label)
    if reap_unsynced == "keep":
        return f"KEPT orphan '{label}': unpushed work, --reap-unsynced=keep — snapshot+reap not requested"
    return _snapshot_then_reap_orphan(repo, wt_path, branch, label)


def _remove_recoverable_orphan(repo: str, wt_path: str, branch: str, label: str) -> str:
    """Reap an orphan whose work is already on a remote (no snapshot needed)."""
    if _remove_orphan(repo, wt_path, branch):
        return f"Reaped orphan worktree (work already on remote): {label}"
    return f"SKIPPED orphan '{label}': git worktree remove failed"


def _snapshot_then_reap_orphan(repo: str, wt_path: str, branch: str, label: str) -> str:
    """Snapshot an orphan's unique work, then reap — keeping it if the snapshot fails (#706)."""
    snapshot = _snapshot_orphan(repo, wt_path, branch)
    if snapshot is None:
        return f"KEPT orphan '{label}': snapshot did not materialise — refusing to reap (data-loss guard)"
    if _remove_orphan(repo, wt_path, branch):
        return f"Reaped orphan worktree (snapshot at {snapshot}): {label}"
    return f"SKIPPED orphan '{label}': snapshot written to {snapshot} but git worktree remove failed"


def _snapshot_orphan(repo: str, wt_path: str, branch: str) -> Path | None:
    """Capture a recovery artifact for an orphan with unique work, or ``None`` on failure.

    Delegates to the shared Django-free capture primitive (bundle of the branch +
    diff of the working tree). A capture exception keeps the worktree — the
    caller treats ``None`` as "nothing recoverable was written", so the #706
    guard refuses the reap.
    """
    try:
        return capture_worktree_snapshot(Path(repo), wt_path, branch=branch, label="orphan")
    except CommandFailedError as exc:
        logger.warning("orphan snapshot failed for %s (%s): %s — keeping the worktree", wt_path, branch, exc)
        return None


def reap_orphan_raw_worktrees(workspace: Path, *, reap_unsynced: ReapUnsyncedPolicy) -> list[str]:
    """Discover and dispose of raw git worktrees no ``Worktree`` row tracks (#2361).

    For every main clone teatree knows about, every linked worktree whose
    absolute path is NOT in the DB-tracked set is an orphan. Each is classified
    and disposed of by :func:`_reap_one_orphan` under the supplied policy. The
    pass is resilient: a clone whose worktree registry cannot be read (corrupt /
    origin-less) is skipped with a warning rather than aborting the run.
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
            cleaned.append(_reap_one_orphan(repo, wt_path, branch, reap_unsynced=reap_unsynced))
    return cleaned
