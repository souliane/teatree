"""Durable owner cards for PR and live-post waits."""

import hashlib
import logging
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.live_post_approval import canonical_mr_scope

logger = logging.getLogger(__name__)
LIVE_POST_MARKER_PREFIX = "owner-wait:post:"


def canonical_wait_subject(subject: str) -> str:
    clean = subject.strip()
    if "://" not in clean:
        return clean.rstrip("/")
    parts = urlsplit(clean)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _subject_key(subject: str) -> str:
    return hashlib.sha256(canonical_wait_subject(subject).encode()).hexdigest()[:20]


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
    prefix = f"{LIVE_POST_MARKER_PREFIX}{_subject_key(scope)}:"
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
    prefix = f"{LIVE_POST_MARKER_PREFIX}{_subject_key(subject)}:"
    for row in DeferredQuestion.owner_pending().filter(dedupe_marker__startswith=prefix):
        row.mark_stale(f"the live post was approved for {subject}", resolver_id="live_post_approved")
