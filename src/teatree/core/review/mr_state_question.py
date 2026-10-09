"""Ask the OWNER what state a merge request is in, when nothing can decide it.

This is the bot asking its own operator about the operator's own work. It is
NOT a post made as the user to a colleague, so it must never be routed through
or gated by the on-behalf posture — those
govern the user's colleague-facing voice. Arming the publish gate (the shipped
default) would otherwise swallow every one of these questions at exactly the
moment the operator is most careful about what leaves their machine.

:class:`~teatree.core.models.deferred_question.DeferredQuestion` is already that
separate surface: it reaches the owner through
:func:`teatree.core.notify_question_drains.drain_unmirrored_deferred_questions`
over :func:`teatree.core.notify.notify_user`, a bot→owner path with no
``OnBehalfSlackEgress`` anywhere in it. So this module builds a row; it does not
build a channel.

Two bounds keep the surface from becoming noise. The dedupe marker is
``mr-state:<canonical url>`` built from the SAME canonicaliser the review-request
guard and the sanctioned-post command use, so one merge request can hold at most
one open question no matter how many ticks re-derive the ambiguity, and the
marker is provably the same string as every other review-request scope for that
merge request. Above ``MAX_OPEN_QUESTIONS`` open questions the ask
is REFUSED rather than queued, so a backlog of undecidable merge requests cannot
arrive as a flood the owner answers none of; the refused merge request is
re-offered on a later tick once a slot frees.
"""

import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction

from teatree.core.gates.review_request_guard import canonical_mr_url
from teatree.core.models.deferred_question import DeferredQuestion

logger = logging.getLogger(__name__)

_MARKER_PREFIX = "mr-state:"
_HEAD_TAG = re.compile(r"\[head [0-9a-f]{12}\]")
_OWNER_QUESTION_OBSERVER: ContextVar[Callable[[str], None] | None] = ContextVar(
    "owner_question_observer",
    default=None,
)


@contextmanager
def observe_owner_question_creation(observer: Callable[[str], None]) -> Iterator[None]:
    token = _OWNER_QUESTION_OBSERVER.set(observer)
    try:
        yield
    finally:
        _OWNER_QUESTION_OBSERVER.reset(token)


#: Open state questions the owner is asked to hold at once, so a backlog of undecidable
#: merge requests arrives as a pair rather than a flood nobody answers.
MAX_OPEN_QUESTIONS = 2


def mr_state_marker(mr_url: str) -> str:
    """The dedupe scope for *mr_url* — one open question per canonical merge request."""
    return f"{_MARKER_PREFIX}{canonical_mr_url(mr_url)}"


def head_tag(head_sha: str) -> str:
    """The marker a head-bound question carries, so an answer binds to the commit it was asked about."""
    return f"[head {head_sha[:12]}]"


def ask_mr_state(
    *, mr_url: str, reason: str, options: Sequence[str] = (), head_sha: str = ""
) -> DeferredQuestion | None:
    """Queue an owner question about *mr_url*'s state; ``None`` when nothing is asked.

    Returns the already-open row when one exists for this merge request, so a
    re-ask is idempotent and can never be refused by the cap — a merge request
    the owner is already being asked about occupies its slot rather than
    competing for a new one. A *head_sha* ask differs in two ways: a head the
    owner already answered about is never asked about again, and it replaces its
    own kind of open row (see :func:`_replaces`), inheriting that row's slot.
    """
    if head_sha and owner_answer_at_head(mr_url, head_sha=head_sha) is not None:
        return None
    marker = mr_state_marker(mr_url)
    text = _question_text(mr_url=mr_url, reason=reason, head_sha=head_sha)
    open_questions = DeferredQuestion.objects.filter(
        dedupe_marker__startswith=_MARKER_PREFIX,
        answered_at__isnull=True,
        dismissed_at__isnull=True,
    )
    with transaction.atomic():
        already_asked = open_questions.filter(dedupe_marker=marker).first()
        if already_asked is not None:
            if not _replaces(already_asked, text=text, head_sha=head_sha):
                return already_asked
            already_asked.mark_stale("superseded by a question about the merge request's current head")
        elif open_questions.count() >= MAX_OPEN_QUESTIONS:
            logger.info(
                "mr-state question for %s deferred — %s open already (cap %s)",
                mr_url,
                open_questions.count(),
                MAX_OPEN_QUESTIONS,
            )
            return None
        question = DeferredQuestion.record(
            text,
            options_json=_options_json(options),
            dedupe_marker=marker,
        )
    if (observer := _OWNER_QUESTION_OBSERVER.get()) is not None:
        observer(mr_url)
    return question


def _replaces(open_row: DeferredQuestion, *, text: str, head_sha: str) -> bool:
    """Whether a head-bound ask supersedes *open_row*.

    Only another head-bound row, and at the same head only a wording that head
    has not seen, so a blocker that alternates is asked about once, not every pass.
    """
    if not head_sha or open_row.question == text or not _HEAD_TAG.search(open_row.question):
        return False
    if head_tag(head_sha) not in open_row.question:
        return True
    return not DeferredQuestion.objects.filter(dedupe_marker=open_row.dedupe_marker, question=text).exists()


def retire_mr_state_question(mr_url: str, *, reason: str) -> None:
    """Mark *mr_url*'s open state question stale, freeing its slot under the cap."""
    question = _open_question(mr_url)
    if question is not None:
        question.mark_stale(reason)


def retire_head_bound_question(mr_url: str, *, reason: str) -> None:
    """Mark *mr_url*'s open head-bound question stale; another caller's untagged question stays open."""
    question = _open_question(mr_url)
    if question is not None and _HEAD_TAG.search(question.question):
        question.mark_stale(reason)


def owner_answer_at_head(mr_url: str, *, head_sha: str) -> DeferredQuestion | None:
    """The owner's answer to a question about *mr_url* asked at *head_sha*."""
    return DeferredQuestion.objects.filter(
        dedupe_marker=mr_state_marker(mr_url),
        answered_at__isnull=False,
        question__contains=head_tag(head_sha),
    ).first()


def _open_question(mr_url: str) -> DeferredQuestion | None:
    return DeferredQuestion.pending().filter(dedupe_marker=mr_state_marker(mr_url)).first()


def _question_text(*, mr_url: str, reason: str, head_sha: str) -> str:
    subject = f"{mr_url} {head_tag(head_sha)}" if head_sha else mr_url
    return f"I cannot determine the state of {subject} — {reason} How should I treat it?"


def _options_json(options: Sequence[str]) -> str:
    return json.dumps([{"label": option} for option in options]) if options else ""
