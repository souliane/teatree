"""Emit records for work-bearing worktrees teatree never registered (#4579).

:func:`~teatree.core.worktree.worktree_done.collect_emit_records` assembles a record per
``Worktree`` ROW. A dispatched agent's bare ``git worktree add`` creates no row, so its
checkout reached ``workspace emit`` through nothing at all — and ``emit`` is the enforcement
arm of "no work-bearing state is terminal". This module is the other source set, built from
the same discovery the orphan reaper uses, so the two cannot disagree about which checkouts
hold work.

Only work-bearing orphans are emitted. A clean orphan whose commits are on a remote is
recoverable from that remote, so reporting it would drown the signal the operator reads —
and the reaper deletes it unattended anyway. Deliberately no ``git fetch``: this is a
read-only pass, and the ledger path does not fetch either, so a stale tracking ref can make
a commits-only orphan read as pushed until the next ``clean-all`` refreshes the refs. The
uncommitted-work arm is a purely local probe and is unaffected.
"""

import logging
from pathlib import Path

from teatree.core.cleanup.clean_ignore import is_clean_ignored
from teatree.core.cleanup.cleanup import _EffectiveTarget
from teatree.core.cleanup.cleanup_emit import CleanupEmitRecord, banned_terms_status
from teatree.core.cleanup.orphan_checkouts import OrphanCheckout, discover_orphan_checkouts, orphan_has_unique_work
from teatree.core.cleanup.process_table import ProcessTable, read_process_table
from teatree.core.cleanup.working_tree_dirt import WorkingTreeDirt, working_tree_dirt
from teatree.core.worktree.branch_classification import INCONCLUSIVE_SOURCE, branch_redundancy, effective_default_target
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

_ORPHAN_KIND = "orphan-worktree"
_LIVENESS_REASON = "a live process is working inside this unregistered checkout"


def collect_orphan_emit_records(workspace: Path) -> list[CleanupEmitRecord]:
    """One record per work-bearing orphan checkout, for the judgment skill to route.

    A ``clean_ignore`` match is withheld exactly as the ledger pass withholds one: the
    operator has already ruled the branch never-reap, so there is nothing to route.
    """
    scan = discover_orphan_checkouts(workspace)
    for gap in scan.gaps:
        logger.warning("orphan emit could not enumerate a clone, so its checkouts went unreported: %s", gap)
    if not scan.orphans:
        return []
    processes = read_process_table()
    records = (
        _work_bearing_record(orphan, processes) for orphan in scan.orphans if not is_clean_ignored(orphan.branch)
    )
    return [record for record in records if record is not None]


def _probe_target(orphan: OrphanCheckout) -> _EffectiveTarget:
    """The dirt probe's teardown target, mirroring the present-dir branch of ``_effective_target``."""
    detached = orphan.branch == git.DETACHED_HEAD
    return _EffectiveTarget(
        ref=git.DETACHED_HEAD,
        probe_repo=orphan.path,
        branch_to_delete=None if detached else orphan.branch,
        label=orphan.branch,
    )


def _work_bearing_record(orphan: OrphanCheckout, processes: ProcessTable) -> CleanupEmitRecord | None:
    """The record for ``orphan``, or ``None`` when it holds nothing that could be lost.

    The two cheap local probes decide first, so a clean orphan never pays for the
    banned-terms diff scan. An UNPROVEN dirt probe counts as work-bearing: silent absence
    from this handoff is the failure the module exists to fix, so a checkout nothing could
    read is reported rather than dropped.
    """
    dirt = working_tree_dirt(orphan.path, _probe_target(orphan))
    if not (dirt.paths or not dirt.proven or orphan_has_unique_work(orphan.repo, orphan.branch, orphan.path)):
        return None
    return _build_record(orphan, dirt=dirt, live=processes.holds(Path(orphan.path)))


def _build_record(orphan: OrphanCheckout, *, dirt: WorkingTreeDirt, live: bool) -> CleanupEmitRecord:
    """Assemble the structured handoff, resolving redundancy and banned terms like the ledger path."""
    detached = orphan.branch == git.DETACHED_HEAD
    probe_repo = orphan.path if detached else orphan.repo
    ref = git.DETACHED_HEAD if detached else orphan.branch
    default_target = effective_default_target(orphan.repo)
    verdict = branch_redundancy(probe_repo, ref, default_target)
    try:
        texts = [
            git.run_strict(repo=probe_repo, args=["log", f"{default_target}..{ref}", "--format=%B"]),
            git.run_strict(repo=probe_repo, args=["diff", f"{default_target}...{ref}"]),
        ]
    except CommandFailedError:
        # STRICT so the failure is real: unreadable content emits "unknown", which the
        # judgment skill treats conservatively rather than as "scanned clean".
        texts = []
    status, found = banned_terms_status(texts)
    return CleanupEmitRecord(
        path=orphan.path,
        branch=orphan.branch,
        kind=_ORPHAN_KIND,
        unique_commit_shas=verdict.unique_shas,
        uncommitted_paths=list(dirt.paths),
        merged_with_post_merge_work=verdict.merged_with_post_merge_work,
        content_verified=dirt.proven and verdict.source != INCONCLUSIVE_SOURCE,
        verdict_source=verdict.source,
        banned_terms_status=status,
        banned_terms_found=found,
        liveness=_LIVENESS_REASON if live else "",
        last_commit_date=git.run(repo=probe_repo, args=["log", "-1", "--format=%cI", ref]),
        owner=git.run(repo=probe_repo, args=["log", "-1", "--format=%an", ref]),
    )
