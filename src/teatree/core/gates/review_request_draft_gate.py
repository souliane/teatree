"""Refuse a review-request broadcast for a DRAFT MR at the post chokepoint (#1084 follow-up).

A draft MR is not ready for review, so ``review_request_check`` and
``review_request_post`` refuse it BEFORE the dedup claim. The probe is
fail-open: only a code-host-CONFIRMED draft refuses — an unparsable URL, an
unconfigured host, or a read error resolves to "not a draft" so a flaky forge
read can never block a legitimate post.
"""

import logging

from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


def is_draft_mr(mr_url: str, *, overlay_name: str = "") -> bool:
    """True only when the code host confirms *mr_url* is a draft; unknown ⇒ False.

    *overlay_name* names the overlay whose forge credentials answer the probe,
    for callers that do not run under the CLI's ``T3_OVERLAY_NAME`` bridge —
    the in-process MCP surface registers every overlay, so an ambient
    ``code_host_from_overlay()`` there resolves no host and the gate reports
    "not a draft" for a genuinely-draft MR (the same mis-routing class as the
    guard's ``no_review_channel_or_token``). Blank keeps the ambient default.
    """
    ref = pr_ref_from_url(mr_url)
    if ref is None:
        return False
    from teatree.core.backend_factory import code_host_from_overlay  # noqa: PLC0415 — deferred: call-time backend build

    host = code_host_from_overlay(overlay_name or None)
    if host is None:
        return False
    try:
        return bool(host.fetch_pr_is_draft(slug=ref.slug, pr_id=ref.pr_id))
    except Exception as exc:  # noqa: BLE001 — a draft probe must never crash the post path
        logger.warning("review_request draft gate: is-draft probe failed for %s: %s", mr_url, exc)
        return False
