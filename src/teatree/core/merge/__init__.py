"""The §17.4 merge keystone — loop-executes side (BLUEPRINT §17.4).

Package facade re-exporting the cross-package public surface so callers import
from ``teatree.core.merge`` while each symbol keeps an explicit defining module
(``errors`` / ``ci_rollup`` / ``pr_slug_resolution`` / ``authorization`` /
``execution``). This is the package's public API, not a backward-compat alias —
``mock.patch`` targets name the defining submodule, never this facade.
"""

from teatree.core.merge.authorization import MergePrecheck, _assert_clear_authorized
from teatree.core.merge.ci_rollup import (
    fetch_live_head_sha,
    fetch_pr_is_draft,
    fetch_pr_merge_state,
    fetch_required_checks_status,
)
from teatree.core.merge.errors import MergeHeadMovedError, MergePreconditionError, MergeReplayError, MergeTransientError
from teatree.core.merge.execution import (
    MergeOutcome,
    assert_merge_preconditions,
    execute_bound_merge,
    merge_ticket_pr,
    record_merge_and_advance,
)
from teatree.core.merge.pr_slug_resolution import _GIT_BRANCH_PREFIXES, _looks_like_owner_repo, resolve_pr_repo_slug

__all__ = [
    "_GIT_BRANCH_PREFIXES",
    "MergeHeadMovedError",
    "MergeOutcome",
    "MergePrecheck",
    "MergePreconditionError",
    "MergeReplayError",
    "MergeTransientError",
    "_assert_clear_authorized",
    "_looks_like_owner_repo",
    "assert_merge_preconditions",
    "execute_bound_merge",
    "fetch_live_head_sha",
    "fetch_pr_is_draft",
    "fetch_pr_merge_state",
    "fetch_required_checks_status",
    "merge_ticket_pr",
    "record_merge_and_advance",
    "resolve_pr_repo_slug",
]
