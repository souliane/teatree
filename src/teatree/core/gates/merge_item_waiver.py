"""An owner's recorded waiver of one LLM merge-critic item, per ticket.

A merge item that keeps failing after repeated rounds can be accepted by the owner instead of
spending another round. The waiver is keyed by the registry slug, so only ``llm_items("merge")``
(``test_value``, ``cleanliness``) qualify; a deterministic, security or leak gate is never waivable.
It outlives heads: it covers the ticket, not one shipped tree.
"""

from django.db import transaction
from django.utils import timezone

from teatree import config
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker, read_action
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, match_action_option, safe_ticket_card_title
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.reviewer_identity import is_non_reviewer_role, normalize_reviewer_identity
from teatree.core.models.ticket import Ticket
from teatree.core.owner_action_auth import direct_owner_action
from teatree.core.review.critic_rubric import _MERGE_TRANSITION, llm_items

_WAIVERS_KEY = "merge_item_waivers"


class MergeItemWaiverError(ValueError):
    """A merge-item waiver was refused."""


def _validated_item(slug: str, reason: str) -> str:
    slug = slug.strip()
    item_slugs = [item.slug for item in llm_items(_MERGE_TRANSITION)]
    if slug not in item_slugs:
        msg = f"{slug!r} is not an LLM merge item ({', '.join(item_slugs)}); no other gate can be waived"
        raise MergeItemWaiverError(msg)
    if not reason.strip():
        msg = "a non-empty waiver reason is required"
        raise MergeItemWaiverError(msg)
    return slug


def request_merge_item_waiver(ticket: Ticket, *, slug: str, reason: str) -> DeferredQuestion:
    """Queue the owner decision that can authorize one item waiver."""
    slug = _validated_item(slug, reason)
    label = {"test_value": "test evidence", "cleanliness": "code cleanliness"}[slug]
    action = ActionRequest(
        kind=OwnerActionKind.MERGE_ITEM_WAIVER, target=str(ticket.pk), binding=slug, note=reason.strip()
    )
    title = safe_ticket_card_title(ticket.short_description, ticket.extra, "this ticket")
    question = f"Should I accept the merge critic's {label} finding for {title}?"
    card = QuestionCard(
        decision=OwnerDecision.PRODUCT_SCOPE,
        checked=(
            f"Only the {label} assessment would be waived for this ticket.",
            "Deterministic, security and leak checks remain required.",
        ),
        blocker="Only the owner can accept this assessment.",
        why="The merge critic's assessment has not passed after repeated rounds.",
        options=(
            CardOption("Accept", "Record this item waiver and its reason.", recommended=True),
            CardOption("Keep", "The merge critic must pass this item."),
        ),
        action=action,
    ).with_quote(question, reason)
    return DeferredQuestion.record(question, dedupe_marker=action_marker(action), card=card)


def _owner_answer(question: DeferredQuestion, ticket: Ticket, slug: str, reason: str) -> str:
    row = DeferredQuestion.objects.select_for_update().get(pk=question.pk)
    action = read_action(row.evidence)
    answer = DeferredQuestionAudit.objects.owner_answer(row)
    option = match_action_option(row.options_json, row.answer_text)
    scope_matches = (
        action is not None
        and action["kind"] == OwnerActionKind.MERGE_ITEM_WAIVER
        and action["target"] == str(ticket.pk)
        and action["binding"] == slug
        and action.get("note") == reason.strip()
    )
    provenance_matches = (
        answer is not None
        and answer.note == "button"
        and answer.resolver_id.startswith("slack:")
        and row.resolved_via == DeferredQuestion.ResolvedVia.SLACK
    )
    if not (
        scope_matches
        and provenance_matches
        and action is not None
        and row.dedupe_marker == action_marker(action)
        and option is not None
        and option.recommended
    ):
        msg = "a matching owner button answer is required to waive this item"
        raise MergeItemWaiverError(msg)
    return answer.resolver_id


def waive_merge_item(
    ticket: Ticket, *, slug: str, waived_by: str, reason: str, owner_question: DeferredQuestion | None = None
) -> dict[str, str]:
    """Record the owner's waiver of *slug* on *ticket*; a repeat for the same item keeps the first."""
    slug = _validated_item(slug, reason)
    identity = waived_by.strip()
    if not identity or is_non_reviewer_role(identity):
        msg = "an owner identity is required; the maker, coding-agent and loop role identities cannot waive an item"
        raise MergeItemWaiverError(msg)
    with transaction.atomic():
        if owner_question is not None:
            if identity != _owner_answer(owner_question, ticket, slug, reason):
                msg = "the waiver identity does not match the owner's answer"
                raise MergeItemWaiverError(msg)
        else:
            if not direct_owner_action():
                msg = "only a direct owner command or an owner button answer can waive a merge item"
                raise MergeItemWaiverError(msg)
            try:
                owner_aliases = {
                    normalize_reviewer_identity(alias)
                    for alias in config.get_effective_settings(ticket.overlay or None).user_identity_aliases
                }
            except Exception as exc:
                msg = "owner identities could not be read; no merge item was waived"
                raise MergeItemWaiverError(msg) from exc
            if normalize_reviewer_identity(identity) not in owner_aliases:
                msg = "the waiver identity is not a configured owner alias; set user_identity_aliases first"
                raise MergeItemWaiverError(msg)
        locked = Ticket.objects.select_for_update().get(pk=ticket.pk)
        if existing := (locked.extra.get(_WAIVERS_KEY) or {}).get(slug):
            ticket.extra = locked.extra
            return existing
        waiver = {"waived_by": identity, "waived_at": timezone.now().isoformat(), "reason": reason.strip()}
        ticket.merge_extra(merge_into_dicts={_WAIVERS_KEY: {slug: waiver}})
        return waiver


def waived_merge_items(ticket: Ticket) -> frozenset[str]:
    return frozenset(ticket.extra.get(_WAIVERS_KEY) or {})
