"""Probe snapshots translated into the token-health cache's value object."""

import datetime as dt

from teatree.core.models.anthropic_token_usage import REJECTED_STATUS, TokenHealthReading, UnifiedVerdict
from teatree.llm.rate_limits import MeteredKeySnapshot, RateLimitSnapshot


def reading_from(snapshot: RateLimitSnapshot) -> TokenHealthReading:
    """Translate a foundation ``RateLimitSnapshot`` into the domain cache's value object."""
    return TokenHealthReading(
        organization_id=snapshot.organization_id,
        utilization_5h=snapshot.unified_5h_utilization,
        utilization_7d=snapshot.unified_7d_utilization,
        status_5h=snapshot.unified_5h_status,
        status_7d=snapshot.unified_7d_status,
        reset_5h=snapshot.unified_5h_reset,
        reset_7d=snapshot.unified_7d_reset,
        verdict=UnifiedVerdict(status=snapshot.unified_status, representative_claim=snapshot.representative_claim),
    )


def reading_from_metered(snapshot: MeteredKeySnapshot) -> TokenHealthReading:
    """Translate a metered API-key status into the domain cache's value object.

    A standard key exposes no dollar balance and no unified windows, so the routing
    verdict rides the credit flag: an out-of-credits key is recorded with a rejected 7d
    status — exactly the exhaustion signal the selector already refuses to route to.
    """
    return TokenHealthReading(
        organization_id=snapshot.organization_id,
        utilization_5h=None,
        utilization_7d=None,
        status_5h="",
        status_7d=REJECTED_STATUS if snapshot.out_of_credits else "",
        reset_5h=None,
        reset_7d=None,
    )


def unverified_exhaustion(*, resets_at: dt.datetime, weekly: bool) -> TokenHealthReading:
    """The fallback verdict for an unprobeable account — a weekly hit blocks 7d, else 5h.

    Nothing here is measured, so ``verified=False`` caps its trust at ``HEALTH_TTL``: a
    wrong window or a wrong account self-corrects in minutes instead of days.
    """
    return TokenHealthReading(
        organization_id="",
        utilization_5h=None if weekly else 1.0,
        utilization_7d=1.0 if weekly else None,
        status_5h="",
        status_7d=REJECTED_STATUS if weekly else "",
        reset_5h=None if weekly else resets_at,
        reset_7d=resets_at if weekly else None,
        verified=False,
    )
