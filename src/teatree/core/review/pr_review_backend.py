"""Resolve WHICH reviewer executes a self-authored-PR cold review.

The self-PR review board admits every self-authored open PR; this decides who
reviews it. Two rules, and the whole module exists to keep them apart:

*   an explicit ``claude`` / ``codex`` pin is returned as written — an operator
    who named a reviewer is told it is unavailable rather than discovering later
    that something else reviewed their diffs;
*   ``auto`` prefers codex but degrades to claude the moment codex cannot serve
    (no binary on PATH, or a live quota cooldown), because a degraded review is
    worth more than none.

The cooldown consult is what makes ``auto`` cheap: without it, an exhausted
account is rediscovered by burning a dispatch every tick.
"""

import logging
import shutil

from teatree.config import PrReviewBackend, get_effective_settings
from teatree.core.models.review_backend_cooldown import ReviewBackendCooldown

logger = logging.getLogger(__name__)

_CODEX_BINARY = "codex"


def codex_is_available(*, overlay: str = "") -> bool:
    """Whether codex can serve a review right now — installed and not cooling down.

    Fails CLOSED on an unreadable cooldown. The read runs inside a scanner builder,
    which the loop calls in contexts with no usable DB, and an unanswerable "is it
    cooling?" must not read as "no cooldown, use codex" — that is precisely the
    exhausted-account case the cooldown exists to avoid re-probing. Claude is
    always available, so the conservative branch costs a backend preference and
    never a review.
    """
    if shutil.which(_CODEX_BINARY) is None:
        return False
    try:
        return not ReviewBackendCooldown.is_cooling(backend=PrReviewBackend.CODEX.value, overlay=overlay)
    except Exception:
        logger.warning("codex cooldown unreadable — resolving the review backend to claude", exc_info=True)
        return False


def resolve_pr_review_backend(overlay: str = "") -> PrReviewBackend:
    """The backend that will review *overlay*'s self-authored PRs.

    Never returns :attr:`PrReviewBackend.AUTO` — ``auto`` is the operator's
    instruction to choose, so it is resolved here to the backend that actually
    runs. An explicit pin passes through untouched.
    """
    configured = get_effective_settings(overlay or None).pr_review_backend
    if configured is not PrReviewBackend.AUTO:
        return configured
    return PrReviewBackend.CODEX if codex_is_available(overlay=overlay) else PrReviewBackend.CLAUDE
