"""Stamp a landed PR's ledger rows MERGED at the one chokepoint every merge route crosses."""

import logging

from teatree.core.models import PullRequest
from teatree.utils.pr_ref import PrRef

logger = logging.getLogger(__name__)


def record_pr_landed(ref: PrRef, merged_sha: str) -> str:
    """Stamp *ref*'s ledger rows MERGED; return *merged_sha* unchanged (#3984).

    THE "a PR landed" recorder, deliberately at the one chokepoint every merge route
    already crosses. Recording it only in the keystone post hook left the sweep's two
    no-CLEAR routes — the solo-overlay bypass and the uv-audit raw fallback, both of
    which reach the forge through :meth:`PrApiClient.merge_pr_bound` → here —
    writing nothing, so a PR the sweep demonstrably merged kept a row reading ``open``
    and every consumer asking "has this ticket's PR landed?" answered ``False``. Placing
    it here means a future third route cannot silently reintroduce that.

    Record-and-proceed: the forge merge is already irreversible, so a ledger failure is
    logged and never raised — propagating it would strand the keystone post hook the
    caller still has to run, which loses more than a stale row the reconcile pass
    (:meth:`PullRequestQuerySet.reconcile_forge_states`) then converges.
    """
    try:
        PullRequest.objects.record_forge_merge(slug=ref.slug, pr_id=ref.pr_id)
    except Exception:
        logger.exception(
            "merge landed for %s#%s but its PullRequest ledger row could not be marked merged",
            ref.slug,
            ref.pr_id,
        )
    return merged_sha
