"""The seam that turns a failed review-backend run into a durable cooldown.

Two halves that must not drift: the pure classifier
(:func:`~teatree.failure_signatures.quota_exhausted`, which decides whether a
run's stderr shows account exhaustion) and the durable record
(:class:`~teatree.core.models.review_backend_cooldown.ReviewBackendCooldown`,
which the `auto` backend resolution reads). This module is the ONE place they
meet, so every caller that runs a review backend trips the cooldown identically
and none of them re-implements the "is this exhaustion?" question.
"""

from teatree.config import get_effective_settings
from teatree.core.models.review_backend_cooldown import ReviewBackendCooldown
from teatree.failure_signatures import quota_exhausted


def record_quota_exhaustion(*, backend: str, overlay: str = "", returncode: int, stderr: str) -> str:
    """Park *backend* when its run failed for want of quota; return the signature.

    ``""`` means the run was not an exhaustion and nothing was recorded — a
    successful run, or a failure with an ordinary error. The caller passes the
    run's stderr, never its review body: see :func:`quota_exhausted` for why that
    distinction is the whole of the precision here.
    """
    signature = quota_exhausted(returncode=returncode, stderr=stderr)
    if not signature:
        return ""
    ReviewBackendCooldown.start(
        backend=backend,
        overlay=overlay,
        signature=signature,
        ttl_hours=get_effective_settings(overlay or None).review_backend_cooldown_hours,
    )
    return signature
