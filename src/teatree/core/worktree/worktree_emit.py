"""What the reaper knows about one row, and the record the judgment skill reads.

Split out of :mod:`teatree.core.worktree.worktree_done` so the reaping DECISION and
the row's evidence stay separate concerns. Everything here is read-only: it resolves
a row's effective branch, runs the landed ladder over it ONCE, and renders the result
as the structured handoff record. Nothing in this module destroys anything.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.core.cleanup.cleanup import _effective_target, _EffectiveTarget, _resolve_worktree_path
from teatree.core.cleanup.cleanup_emit import CleanupEmitRecord, banned_terms_status
from teatree.core.cleanup.working_tree_dirt import working_tree_dirt
from teatree.core.models import Worktree
from teatree.core.worktree.branch_classification import (
    INCONCLUSIVE_SOURCE,
    RedundancyVerdict,
    branch_redundancy,
    effective_default_target,
)
from teatree.core.worktree.clone_paths import resolve_clone_path
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout
from teatree.utils import git
from teatree.utils.run import CommandFailedError

_CLONE_UNRESOLVABLE_SOURCE = "clone-unresolvable"


def _effective_default_target(repo: Path) -> str:
    """Resolve ``repo``'s REAL default branch as an ``origin/<default>`` ref.

    Thin ``Path``-taking adapter over the shared
    :func:`branch_classification.effective_default_target` so done-detection, the
    redundancy/emit probes, and :func:`cleanup._raise_if_genuinely_ahead` all
    resolve the base the SAME way (a ``master``/``develop``-default repo is never
    measured against a base it does not have). Fail-safe to ``origin/main`` on an
    unresolvable default — the downstream content gate fails CLOSED there.
    """
    return effective_default_target(str(repo))


@dataclass(frozen=True, slots=True)
class _RowProbes:
    """The resolutions the reaper makes ONCE per row, for every step below to reuse.

    ``verdict`` is ``None`` for a detached HEAD, which names no branch any rung of
    the landed ladder can read.
    """

    workspace: Path
    target: _EffectiveTarget
    verdict: RedundancyVerdict | None


def _resolve_row_probes(workspace: Path, repo_main: Path, wt_path: str, worktree: Worktree) -> _RowProbes:
    """Resolve the row's effective branch and run the landed ladder over it — once.

    The ladder's top rung is a forge round-trip, and three separate consumers used
    to run the whole thing per row; one shared verdict is the same answer at a
    third of the cost.
    """
    target = _effective_target(str(repo_main), wt_path, worktree)
    if target.branch_to_delete is None:
        return _RowProbes(workspace=workspace, target=target, verdict=None)
    verdict = branch_redundancy(str(repo_main), target.branch_to_delete, _effective_default_target(repo_main))
    return _RowProbes(workspace=workspace, target=target, verdict=verdict)


def _verdict_provenance(repo_main: Path, verdict: RedundancyVerdict) -> tuple[bool, str]:
    """Did a content probe actually PROVE this verdict, and which layer decided?

    Without this, an empty ``unique_commit_shas`` means two opposite things — the
    tip was proven to hold nothing unique, or nothing could be probed at all — and
    the judgment skill routes the first to DELETE. A repo the shared checkout
    probe cannot confirm (a row whose ``clone_path`` outlived its clone) makes
    every git answer below it meaningless, so it reports its own source rather
    than the verdict's.
    """
    if probe_checkout(repo_main) is not CheckoutState.CHECKOUT:
        return False, _CLONE_UNRESOLVABLE_SOURCE
    return verdict.source != INCONCLUSIVE_SOURCE, verdict.source


def _build_emit_record(
    worktree: Worktree, *, workspace: Path, liveness: str, probes: _RowProbes | None = None
) -> CleanupEmitRecord:
    """Assemble the structured handoff record for a NOT-auto-deleted worktree.

    Resolves the current-tip redundancy (for ``unique_commit_shas`` +
    ``merged_with_post_merge_work``), its provenance (:func:`_verdict_provenance`,
    so an unprobeable item never emits the proven-redundant shape), the WORKING
    TREE's uncommitted delta, the banned-terms status of the unique content, the
    tip author/date, and the liveness reason — everything the judgment skill needs
    to route the item without re-probing git itself.

    The working-tree read is what makes the record agree with the caller that
    builds it. This function is reached from the KEEP branches of the reap pass,
    including the one whose keep-reason IS uncommitted work — and a delta that was
    never committed is invisible to every commit probe above, so without it the
    record described a clean, redundant worktree while the CLI beside it printed
    "salvage, do not wipe". A checkout kept for its dirt now emits that dirt.
    """
    wt_path = _resolve_worktree_path(workspace, worktree)
    repo_main = resolve_clone_path(workspace, worktree) or workspace / worktree.repo_path
    target = probes.target if probes else _effective_target(str(repo_main), wt_path, worktree)
    ref = target.branch_to_delete or worktree.branch
    probe_repo = str(repo_main)
    default_target = _effective_default_target(Path(repo_main))
    verdict = (probes.verdict if probes else None) or branch_redundancy(probe_repo, ref, default_target)
    content_verified, verdict_source = _verdict_provenance(Path(repo_main), verdict)
    try:
        texts = [
            git.run_strict(repo=probe_repo, args=["log", f"{default_target}..{ref}", "--format=%B"]),
            git.run_strict(repo=probe_repo, args=["diff", f"{default_target}...{ref}"]),
        ]
    except CommandFailedError:
        # STRICT so the failure is real, not a lenient "" degrade. Unreadable
        # content emits banned_terms_status "unknown" — the judgment skill treats
        # an unknown-scan item conservatively (clean before salvage), never as
        # "scanned clean".
        texts = []
    status, found = banned_terms_status(texts)
    owner = git.run(repo=probe_repo, args=["log", "-1", "--format=%an", ref])
    last_date = git.run(repo=probe_repo, args=["log", "-1", "--format=%cI", ref])
    return CleanupEmitRecord(
        path=wt_path,
        branch=worktree.branch,
        kind="worktree",
        unique_commit_shas=verdict.unique_shas,
        uncommitted_paths=list(working_tree_dirt(wt_path, target).paths),
        merged_with_post_merge_work=verdict.merged_with_post_merge_work,
        content_verified=content_verified,
        verdict_source=verdict_source,
        banned_terms_status=status,
        banned_terms_found=found,
        liveness=liveness,
        last_commit_date=last_date,
        owner=owner,
    )


__all__ = [
    "_RowProbes",
    "_build_emit_record",
    "_effective_default_target",
    "_resolve_row_probes",
]
