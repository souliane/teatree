"""Reviewer state vocabulary — a pure leaf the models read without an up-edge.

``Ticket`` records ``last_review_state`` from this enum; before #2385 PR-2a it
imported ``ReviewState`` from ``teatree.core.backend_protocols`` via a deferred
function-scoped import (an intra-``core`` up-edge, since the models are the
lowest stratum). Moving the enum DOWN into ``modelkit`` (``depends_on = []``)
lets the models import it at module level with no up-edge. ``backend_protocols``
re-exports it (identity-preserved) so every external consumer — the github /
gitlab backends, the reviewer scanner, the loop — keeps importing the same
object from the same path.
"""

from enum import StrEnum


class ReviewState(StrEnum):
    """A reviewer's current state on a single pull/merge request.

    Used by ``CodeHostBackend.get_review_state`` and by
    ``ReviewerPrsScanner`` to detect approval dismissals — e.g. when a
    forge invalidates a prior approval on force-push, or when a reviewer
    is re-requested after being dismissed.
    """

    NONE = "none"
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    DISMISSED = "dismissed"
    # The reviewer concluded an external review with no postable/approvable
    # action (e.g. a bot MR there is nothing to comment on or approve).
    # Distinct from APPROVED so the dedup never hides a future genuine
    # review, yet terminal so the reviewing task stops re-queueing (#1077).
    REVIEWED_NO_ACTION = "reviewed_no_action"
