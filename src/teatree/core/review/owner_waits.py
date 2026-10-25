"""Durable owner cards for PR and live-post waits."""

import hashlib
import logging
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, safe_ticket_card_title
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.live_post_approval import canonical_mr_scope

logger = logging.getLogger(__name__)


def canonical_wait_subject(subject: str) -> str:
    clean = subject.strip()
    if "://" not in clean:
        return clean.rstrip("/")
    parts = urlsplit(clean)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _subject_key(subject: str) -> str:
    return hashlib.sha256(canonical_wait_subject(subject).encode()).hexdigest()[:20]


def _held_card(subject: str, title: str) -> QuestionCard:
    safe_title = safe_ticket_card_title(title, {}, "this pull request")
    return QuestionCard(
        decision=OwnerDecision.ARCHITECTURE,
        checked=(
            f"The reviewed pull request is ready here: {subject}.",
            f"Its title is {safe_title}.",
        ),
        blocker="The merge waits for your review.",
        why=f"Read <{subject}|{safe_title}> before deciding whether to merge it.",
        options=(
            CardOption("Review it", "Open it and use the usual merge route if you approve.", recommended=True),
            CardOption("Keep holding", "The merge remains held."),
            CardOption("Close it", "Close the pull request separately if you reject it."),
        ),
        subject=subject,
    )


def ask_owner_to_read(pr_url: str, title: str, head_sha: str) -> DeferredQuestion | None:
    subject = canonical_wait_subject(pr_url)
    if not subject or not head_sha.strip():
        logger.error("held PR card lacks a subject or reviewed head")
        return None
    question = "What do you want to do with this pull request after reviewing it?"
    card = _held_card(subject, title)
    prefix = f"owner-wait:held-pr:{_subject_key(subject)}:"
    marker = f"{prefix}{head_sha.strip()[:20]}"
    try:
        row = DeferredQuestion.record(question, dedupe_marker=marker, card=card)
    except DeferredQuestionError:
        logger.exception("held PR card refused for %s", subject)
        return None
    for previous in DeferredQuestion.owner_pending().filter(dedupe_marker__startswith=prefix).exclude(pk=row.pk):
        previous.mark_stale(f"the reviewed head changed for {subject}", resolver_id="held_pr_new_head")
    return row


def _post_card(subject: str) -> QuestionCard:
    review = f"<{subject}|this review>" if "://" in subject else "this review"
    return QuestionCard(
        decision=OwnerDecision.PUBLIC_POST,
        checked=(
            f"A live review comment was requested for {subject}."
            if subject
            else "A live review comment was requested.",
            "The comment text is not included in this notice.",
        ),
        blocker="A colleague-visible post needs your approval.",
        why=f"Only a verified approval message from you can release a live post on {review}.",
        options=(
            CardOption(
                "Approve separately", "Send a separate approval message before any live post.", recommended=True
            ),
            CardOption("Keep it draft", "No live comment will be published from this request."),
        ),
        subject=subject,
    )


def ask_owner_to_approve_post(mr_url: str, *, review_url: str = "") -> DeferredQuestion | None:
    scope = canonical_mr_scope(mr_url)
    if not scope:
        logger.error("live post card lacks a review subject")
        return None
    subject = canonical_wait_subject(review_url or mr_url) if "://" in (review_url or mr_url) else ""
    question = "Will you send a separate approval message before a live review comment?"
    card = _post_card(subject)
    prefix = f"owner-wait:post:{_subject_key(scope)}:"
    resolved = (
        DeferredQuestion.objects.filter(dedupe_marker__startswith=prefix)
        .exclude(answered_at__isnull=True, dismissed_at__isnull=True)
        .count()
    )
    try:
        return DeferredQuestion.record(
            question,
            dedupe_marker=f"{prefix}{resolved}",
            card=card,
        )
    except DeferredQuestionError:
        logger.exception("live post card refused for %s", subject)
        return None


def record_live_post_wait(*, mr_url: str, review_url_supplier: Callable[[], str] | None = None) -> None:
    review_url = ""
    if review_url_supplier is not None:
        try:
            review_url = review_url_supplier()
        except Exception:
            logger.exception("live post could not read the review URL for %s", mr_url)
    try:
        ask_owner_to_approve_post(mr_url, review_url=review_url)
    except Exception:
        logger.exception("live post failed to record an owner card for %s", mr_url)


def settle_live_post_wait(mr_url: str) -> None:
    subject = canonical_mr_scope(mr_url)
    if not subject:
        return
    prefix = f"owner-wait:post:{_subject_key(subject)}:"
    for row in DeferredQuestion.owner_pending().filter(dedupe_marker__startswith=prefix):
        row.mark_stale(f"the live post was approved for {subject}", resolver_id="live_post_approved")
