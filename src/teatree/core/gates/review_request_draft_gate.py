"""Refuse a review-request broadcast for a DRAFT MR at the post chokepoint (#1084 follow-up).

A draft MR is not ready for review, so ``review_request_check`` and
``review_request_post`` refuse it BEFORE the dedup claim. The probe is
three-valued and fails CLOSED: only a code-host-CONFIRMED non-draft lets the post
proceed. An unparsable URL, an unconfigured host, or a read error resolves to
``DraftState.UNKNOWN`` and the post is refused with ``draft_state_unknown``.

Fail-open was the original shape and it silently disarmed the user's own hold
mechanism: marking one MR of a group Draft is how a batch broadcast is held back,
so a probe that answered "not a draft" whenever it could not read the forge fired
exactly the batch the Draft flag existed to stop.
"""

import logging

from teatree.core.backend_protocols import DraftState
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


def draft_state(mr_url: str, *, overlay_name: str = "") -> DraftState:
    """The code host's draft verdict for *mr_url*; anything unreadable is ``UNKNOWN``.

    *overlay_name* names the overlay whose forge credentials answer the probe,
    for callers that do not run under the CLI's ``T3_OVERLAY_NAME`` bridge —
    the in-process MCP surface registers every overlay, so an ambient
    ``code_host_from_overlay()`` there resolves no host (the same mis-routing
    class as the guard's ``no_review_channel_or_token``). Blank keeps the
    ambient default.

    Never raises: the post path must refuse cleanly, not crash.
    """
    ref = pr_ref_from_url(mr_url)
    if ref is None:
        logger.warning("review_request draft gate: unparsable MR URL %s — draft state UNKNOWN", mr_url)
        return DraftState.UNKNOWN
    from teatree.core.backend_factory import code_host_from_overlay  # noqa: PLC0415 — deferred: call-time backend build

    host = code_host_from_overlay(overlay_name or None)
    if host is None:
        logger.warning(
            "review_request draft gate: no code host resolved for %s (overlay %r) — draft state UNKNOWN",
            mr_url,
            overlay_name,
        )
        return DraftState.UNKNOWN
    try:
        return host.fetch_pr_draft_state(slug=ref.slug, pr_id=ref.pr_id)
    except Exception as exc:  # noqa: BLE001 — a draft probe must never crash the post path
        logger.warning("review_request draft gate: is-draft probe failed for %s: %s", mr_url, exc)
        return DraftState.UNKNOWN


_REFUSAL_REASONS: dict[DraftState, str] = {
    DraftState.DRAFT: "draft_mr",
    DraftState.UNKNOWN: "draft_state_unknown",
}


def draft_refusal_reason(mr_url: str, *, overlay_name: str = "") -> str:
    """The machine-readable refusal reason for *mr_url*, or ``""`` when postable.

    One answer for both review-request commands so ``check`` can never predict a
    verdict ``post`` then contradicts.
    """
    return _REFUSAL_REASONS.get(draft_state(mr_url, overlay_name=overlay_name), "")
