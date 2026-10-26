"""An owner's recorded waiver of one LLM merge-critic item, per ticket.

A merge item that keeps failing after repeated rounds can be accepted by the owner instead of
spending another round. The waiver is keyed by the registry slug, so only ``llm_items("merge")``
(``test_value``, ``cleanliness``) qualify; a deterministic, security or leak gate is never waivable.
It outlives heads: it covers the ticket, not one shipped tree.
"""

from django.utils import timezone

from teatree.core.models.reviewer_identity import is_non_reviewer_role
from teatree.core.models.ticket import Ticket
from teatree.core.review.critic_rubric import _MERGE_TRANSITION, llm_items

_WAIVERS_KEY = "merge_item_waivers"


class MergeItemWaiverError(ValueError):
    """A merge-item waiver was refused."""


def waive_merge_item(ticket: Ticket, *, slug: str, waived_by: str, reason: str) -> dict[str, str]:
    """Record the owner's waiver of *slug* on *ticket*; a repeat for the same item keeps the first."""
    item_slugs = [item.slug for item in llm_items(_MERGE_TRANSITION)]
    if slug.strip() not in item_slugs:
        msg = f"{slug!r} is not an LLM merge item ({', '.join(item_slugs)}); no other gate can be waived"
        raise MergeItemWaiverError(msg)
    identity = waived_by.strip()
    if not identity or is_non_reviewer_role(identity):
        msg = "an owner identity is required; the maker, coding-agent and loop role identities cannot waive an item"
        raise MergeItemWaiverError(msg)
    if not reason.strip():
        msg = "a non-empty waiver reason is required"
        raise MergeItemWaiverError(msg)
    slug = slug.strip()
    ticket.refresh_from_db(fields=["extra"])
    if existing := (ticket.extra.get(_WAIVERS_KEY) or {}).get(slug):
        return existing
    waiver = {"waived_by": identity, "waived_at": timezone.now().isoformat(), "reason": reason.strip()}
    ticket.merge_extra(merge_into_dicts={_WAIVERS_KEY: {slug: waiver}})
    return waiver


def waived_merge_items(ticket: Ticket) -> frozenset[str]:
    return frozenset(ticket.extra.get(_WAIVERS_KEY) or {})
