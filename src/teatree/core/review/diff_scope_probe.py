"""The one forge read that arms the #4251 diff-scope gate.

:mod:`teatree.core.review.diff_scope_gate` is pure over an already-fetched
changed-file set; this is the single I/O seam both recording paths (the
``review record`` CLI and the headless orchestrator's verdict recorder) call so
neither hand-rolls the fetch and they cannot disagree about one diff.

Every failure degrades to :meth:`ChangedFileSet.unavailable` and says so in the
log rather than to an empty set: an unread diff proves nothing, so the gate
declines to judge instead of concluding that every cited file is out of scope.
"""

import logging
from collections.abc import Iterable

from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.modelkit.diff_scope import ChangedFileSet, FindingLike, has_blocking_citation
from teatree.utils.pr_ref import PrRef

logger = logging.getLogger(__name__)


def changed_file_set_for(*, slug: str, pr_id: int, host_kind: str = "github") -> ChangedFileSet:
    """The PR's changed-file set from the forge, or an UNAVAILABLE set on any failure."""
    try:
        paths = CodeHostQuery.for_ref(PrRef(slug=slug, pr_id=pr_id, host_kind=host_kind)).pr_changed_paths()
    except Exception:
        logger.exception("diff-scope: changed-paths fetch failed for %s#%d — gate declines to judge", slug, pr_id)
        return ChangedFileSet.unavailable()
    changed = ChangedFileSet.known(paths)
    if not changed.available:
        logger.warning("diff-scope: empty/truncated changed-paths for %s#%d — gate declines to judge", slug, pr_id)
    return changed


def changed_file_set_for_findings(
    findings: Iterable[FindingLike], *, slug: str, pr_id: int, host_kind: str = "github"
) -> ChangedFileSet:
    """The changed-file set, fetched ONLY when a finding could actually trip the gate.

    A clean verdict — or one carrying only nits and PR-level notes — can never be
    refused, so it never pays for a forge round-trip.
    """
    if not has_blocking_citation(findings):
        return ChangedFileSet.unavailable()
    return changed_file_set_for(slug=slug, pr_id=pr_id, host_kind=host_kind)
