"""The §17.4 merge keystone — loop-executes side (BLUEPRINT §17.4).

Package facade re-exporting the cross-package public surface so callers import
from ``teatree.core.merge`` while each symbol keeps an explicit defining module
(``errors`` / ``ci_rollup`` / ``pr_slug_resolution`` / ``authorization`` /
``execution``). This is the package's public API, not a backward-compat alias —
``mock.patch`` targets name the defining submodule, never this facade.
"""

from teatree.core.merge.authorization import MergePrecheck, PresentedApprovals, _assert_clear_authorized
from teatree.core.merge.ci_rollup import CodeHostQuery, classify_required_rollup, failing_required_names
from teatree.core.merge.errors import MergeHeadMovedError, MergePreconditionError, MergeReplayError, MergeTransientError
from teatree.core.merge.execution import (
    MergeOutcome,
    assert_merge_preconditions,
    execute_bound_merge,
    merge_ticket_pr,
    record_merge_and_advance,
)
from teatree.core.merge.head_guard import restore_caller_branch
from teatree.core.merge.pr_slug_resolution import (
    _GIT_BRANCH_PREFIXES,
    _looks_like_owner_repo,
    normalize_repo_slug,
    resolve_pr_repo_slug,
    resolved_repo_slug,
)

__all__ = [
    "_GIT_BRANCH_PREFIXES",
    "CodeHostQuery",
    "MergeHeadMovedError",
    "MergeOutcome",
    "MergePrecheck",
    "MergePreconditionError",
    "MergeReplayError",
    "MergeTransientError",
    "PresentedApprovals",
    "_assert_clear_authorized",
    "_looks_like_owner_repo",
    "assert_merge_preconditions",
    "classify_required_rollup",
    "execute_bound_merge",
    "failing_required_names",
    "merge_ticket_pr",
    "normalize_repo_slug",
    "record_merge_and_advance",
    "resolve_pr_repo_slug",
    "resolved_repo_slug",
    "restore_caller_branch",
]
