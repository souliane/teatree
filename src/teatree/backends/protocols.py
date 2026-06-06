"""Re-export of the backend protocols, now owned by :mod:`teatree.core.backend_protocols`.

The Protocol surface moved into ``teatree.core`` so the domain layer owns the
abstractions it consumes and ``core`` no longer imports ``backends`` (#1922).
Existing ``from teatree.backends.protocols import X`` consumers keep working
through this re-export — ``backends → core`` is the allowed direction.
"""

from teatree.core.backend_protocols import (
    ROLLUP_QUERY_FAILED,
    ApprovalState,
    CIService,
    CodeHostBackend,
    ForgeMergeResult,
    MessageSpec,
    MessagingBackend,
    PrMergeState,
    PrOpenState,
    PullRequestSpec,
    ReviewState,
    rollup_query_failed,
)

__all__ = [
    "ROLLUP_QUERY_FAILED",
    "ApprovalState",
    "CIService",
    "CodeHostBackend",
    "ForgeMergeResult",
    "MessageSpec",
    "MessagingBackend",
    "PrMergeState",
    "PrOpenState",
    "PullRequestSpec",
    "ReviewState",
    "rollup_query_failed",
]
