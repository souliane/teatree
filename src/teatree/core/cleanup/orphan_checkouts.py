"""Raw git worktrees no ``Worktree`` row tracks — one discovery, one unique-work test (#4579).

A dispatched agent's bare ``git worktree add`` leaves a checkout outside teatree's ledger.
:mod:`teatree.core.management.commands._workspace.orphan_worktrees` has always found those and
KEPT the ones holding work; ``workspace emit`` walked the ledger instead and never named them,
so the reaper's own keep-reason was the only record that the work existed — measured on one
deployment as 68 raw worktrees unseen by ``emit``, 10 of them holding 136 uncommitted files.

The reaper and :mod:`teatree.core.worktree.orphan_emit` share the discovery and the
commit-side test here, so the surface that REPORTS stranded work cannot name a different
population from the pass that refuses to reap it. Their DIRT probes differ on purpose: the
reaper needs only a yes/no (:func:`orphan_is_dirty`), while an emit record must name the files,
so emit uses :func:`~teatree.core.cleanup.working_tree_dirt.working_tree_dirt`. That one
diverges only by ignoring regenerable paths (the env cache) — strictly narrower, and a
regenerable-only worktree is KEPT by the reaper either way, so the refinement can only quieten
the report, never authorise a deletion.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.core.cleanup.checkout_registry import candidate_clones, raw_worktree_paths
from teatree.core.cleanup.working_tree_dirt import _porcelain_path, is_orchestration_debris
from teatree.core.models import Worktree
from teatree.core.worktree.branch_classification import branch_redundancy, effective_default_target
from teatree.core.worktree.worktree_paths import paths_match
from teatree.utils import git
from teatree.utils.run import CommandFailedError


@dataclass(frozen=True, slots=True)
class OrphanCheckout:
    """One linked git worktree no ``Worktree`` row claims, and the clone whose registry holds it."""

    repo: str
    path: str
    branch: str


@dataclass(frozen=True, slots=True)
class OrphanScan:
    """Every orphan found across the known clones, and every clone that could not be read.

    ``gaps`` is not decoration: a clone whose registry raised was never looked in, so its
    orphans' absence from ``orphans`` says nothing about whether they exist.
    """

    orphans: tuple[OrphanCheckout, ...]
    gaps: tuple[str, ...]


def db_tracked_worktree_paths() -> list[str]:
    """On-disk paths of every git worktree teatree has a ``Worktree`` row for.

    Returned unresolved so :func:`paths_match` can apply its full symlink-variant set per
    comparison (a bare ``.resolve()`` set misses the ``/private`` literal twin a
    ``git worktree list`` path may carry).
    """
    return [wt.worktree_path for wt in Worktree.objects.all() if wt.worktree_path]


def orphan_checkouts_for_clone(repo: str, tracked: list[str]) -> list[OrphanCheckout]:
    """``repo``'s linked worktrees minus the DB-tracked set, sorted by path.

    Raises :class:`CommandFailedError` when the registry cannot be read — a caller must
    decide what an unread clone means for it, and neither of the two may read the silence
    as "this clone has no orphans".
    """
    return [
        OrphanCheckout(repo=repo, path=wt_path, branch=branch)
        for wt_path, branch in sorted(raw_worktree_paths(repo).items())
        if not any(paths_match(wt_path, candidate) for candidate in tracked)
    ]


def discover_orphan_checkouts(workspace: Path) -> OrphanScan:
    """Every orphan across every clone teatree knows about, unreadable clones reported."""
    tracked = db_tracked_worktree_paths()
    orphans: list[OrphanCheckout] = []
    gaps: list[str] = []
    for repo in sorted(candidate_clones(workspace)):
        try:
            orphans.extend(orphan_checkouts_for_clone(repo, tracked))
        except CommandFailedError as exc:
            gaps.append(f"could not list worktrees of {repo} ({exc})")
    return OrphanScan(orphans=tuple(orphans), gaps=tuple(gaps))


def orphan_is_dirty(wt_path: str) -> bool:
    """Whether the worktree has uncommitted changes (a live, mid-task worktree).

    Fails CLOSED — this gates a checkout removal, so a status that cannot be
    read counts as dirty (keep), and both instruments (``git status`` AND
    ``git diff HEAD``) must agree the tree holds no real work. Only the shared
    orchestration-debris scratch is ignorable; unknown paths are real work.
    """
    try:
        # ``-uall``: an untracked dir must list its files, or debris and real work collapse into one entry.
        porcelain = git.run_strict(repo=wt_path, args=["status", "--porcelain", "--untracked-files=all"])
        diff_head = git.run_strict(repo=wt_path, args=["diff", "HEAD", "--name-only"])
    except CommandFailedError:
        return True
    entries = [_porcelain_path(line) for line in porcelain.splitlines()]
    entries.extend(line.strip() for line in diff_head.splitlines())
    return any(entry and not is_orchestration_debris(entry) for entry in entries)


def orphan_has_unique_work(repo: str, branch: str, wt_path: str) -> bool:
    """Whether ``branch`` carries unmerged work absent from every remote (data loss on removal).

    The #706 primitive (``commits_absent_from_all_remotes``) reports a commit as
    "absent" whenever its SHA is on no remote. A squash-merge rewrites the
    branch's commits into ONE new SHA on the default branch and the source branch
    is typically deleted on merge — the dominant teatree case — so the original
    commit is absent-from-all-remotes by SHA even though its WORK is shipped.
    Treating that as unique work wrongly keeps a resolved orphan. So a branch
    counts as unique unpushed work only when its commits are absent from every
    remote AND the landed ladder (:func:`branch_redundancy`) does NOT find the
    work captured on the repo's default target.

    That target is :func:`effective_default_target` — the SAME resolution
    :func:`~teatree.core.worktree.orphan_emit._build_record` uses, so the two
    cannot disagree about where the work would have landed (they did on a
    ``single_branch_repos``-pinned repo, which emitted the DELETE leaf for a
    checkout this probe had just called work-bearing). It also absorbs an
    unresolvable default: ``git.default_branch`` raises ``RuntimeError`` on a
    clone with no ``origin/HEAD`` and no ``origin/{main,master,development}``,
    and raising here aborted the whole ``workspace emit`` — ledger records
    included. One unreadable clone degrades its own orphan's verdict; it never
    silences the surface.

    A named branch is probed from the shared object store (``repo``); a detached
    HEAD is meaningful only in the worktree dir, so it — and the squash check
    below — are both probed there instead. A linked worktree shares its main
    clone's refs (including ``origin/*``), so running the squash probe against
    ``wt_path`` sees the same remote-tracking state ``repo`` does. Fails CLOSED —
    an inconclusive absence probe (corrupt repo, unknown ref) reads as "has
    unique work" so the worktree is kept, never reaped on uncertainty.

    Assumes the caller has already refreshed ``repo``'s remote-tracking refs where a
    deletion depends on the answer (:func:`reap_orphan_raw_worktrees` does; the read-only
    emit pass deliberately does not fetch). Called against stale refs this returns ``False``
    for work that exists on no remote at all — the misread that reaps unmerged branches.
    """
    probe_repo = wt_path if branch == git.DETACHED_HEAD else repo
    try:
        absent = bool(git.commits_absent_from_all_remotes(probe_repo, branch))
    except CommandFailedError:
        return True
    if not absent:
        return False
    return not branch_redundancy(probe_repo, branch, effective_default_target(repo)).redundant
