"""Which queued question a Slack reply answers — one binder, both consumers.

Two independent consumers drain the same ``PendingChatInjection.loop_unreplied()``
queue: the tick-cadence ``AskUserQuestionReplyScanner`` and the event-driven
``run_slack_answer_cycle`` (an inbound-event wake, ~1s). The cycle wins nearly
every race, and it knew nothing about :class:`DeferredQuestion` — so it stamped
``loop_replied_at``, reacted, and the binder never saw the row. The owner's
answer was acknowledged and dropped. Both consumers now bind through this
module, so the reply→question join cannot drift between them.

The ladder, strongest evidence first:

(a) an explicit ``#<id>`` prefix — a direct question reference in an owner reply — naming a
question mirrored on the reply's own DM;
(b) the reply's ``thread_ts`` — in a ticket root, the newest recorded post before
the reply must name a pending card; in an older card thread, two candidate cards
bind NOTHING and the owner is asked to choose with ``#<id>``;
(c) a top-level reply when exactly ONE live question is mirrored on the channel.

More than one candidate and no thread or id binds NOTHING. The newest-pending-
wins pick it replaces silently answered the wrong row: with 149 questions
mirrored into a single DM, a reply to a three-day-old question resolved
whichever had been posted last, and ✅-acked the owner for it.

Every rung binds only a reply whose recorded author is the question's Slack
addressee: the ticket requester when linked, otherwise the configured owner.

A tap on an option button is the strongest evidence of all: :func:`answer_from_click` joins the
tapped message onto its question by channel and own ``ts``, then the root, and records the option's label.
Every applied answer, typed or tapped, closes the posted card in place.
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.models import OperatorIdentity, PendingChatInjection, TicketDmPost, TicketDmThread
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.question_actions import is_action_question
from teatree.core.models.question_text import options_digest
from teatree.core.notify_targets import question_addressee
from teatree.core.owner_question_message import (
    ALREADY_ANSWERED,
    NO_LONGER_NEEDED,
    answered_line,
    closed_message,
    replace_root,
)
from teatree.loop.inbound_reading import InboundIntent, InboundReader
from teatree.loop.owner_identity import configured_owner_id, is_the_owner

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

# Require a separator so ``#12abc`` cannot answer question 12 with "abc".
_ID_PREFIX_RE = re.compile(r"^\s*#(\d+)(?:[\s:.,\-]+(.*))?$", re.DOTALL)
_DIGIT_RE = re.compile(r"^\s*([1-9][0-9]*)\s*$")
_EXPLICIT_ANSWER_RE = re.compile(r"^\s*answer:\s*(\S(?:.*\S)?)\s*$", re.IGNORECASE | re.DOTALL)
OPTION_ACTION_PREFIX = "owner-question-option-"
_READS_AS_QUESTION = "the text reads as a question"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BoundAnswer:
    """A reply matched to the question it answers, with the text to apply."""

    question: DeferredQuestion
    answer: str
    answered_by: str = ""
    from_click: bool = False
    via: str = DeferredQuestion.ResolvedVia.SLACK


@dataclass(frozen=True, slots=True)
class AmbiguousCards:
    question_ids: tuple[int, ...]


class CardThreadStop(StrEnum):
    CLOSED = "no pending card in this thread"
    NOT_WAITING = "the named question is not waiting"
    OPTIONS_ONLY = "the reply does not match a card option"


@dataclass(frozen=True, slots=True)
class UnmatchedCardThread:
    reason: CardThreadStop
    question_id: int | None = None
    options: tuple[str, ...] = ()


def is_question_addressee(question: DeferredQuestion, user_id: str, backend: "MessagingBackend") -> bool:
    target = question_addressee(question, owner_id=configured_owner_id(backend)).user_id
    if not user_id:
        return False
    if user_id == target:
        return True
    if not question.slack_channel or not question.slack_ts:
        return False
    owner_id = configured_owner_id(backend)
    if (
        user_id != owner_id
        and not OperatorIdentity.objects.filter(
            platform="slack", handle=user_id, user__is_active=True, user__is_superuser=True
        ).exists()
    ):
        return False
    return backend.open_dm(user_id) == question.slack_channel


def bind_reply(
    reply: PendingChatInjection, *, reader: InboundReader, backend: "MessagingBackend", text: str = ""
) -> BoundAnswer | AmbiguousCards | UnmatchedCardThread | None:
    """The question *reply* answers and the answer to apply, or ``None``.

    *text* overrides the row body for a caller that coalesces several rows into
    one logical turn. See the module docstring for the binding ladder.
    """
    owner_reply = is_the_owner(reply.user_id, configured_owner_id(backend))
    body = text or reply.text
    addressed = _addressed(body)
    if addressed is not None:
        question_id, body = addressed
        question = _pending_by_id(question_id, channel=reply.channel)
        key = "id"
        if (
            question is None
            and cards_in_thread(
                channel=reply.channel, thread_ts=reply.thread_ts, before_ts=reply.slack_ts, backend=backend
            )[0]
        ):
            stop = UnmatchedCardThread(CardThreadStop.NOT_WAITING, question_id)
            _log_reply(reply, f"ignored: {stop.reason}")
            return stop if owner_reply else None
    else:
        inferred = _inferred_cards(reply, backend=backend, body=body, reader=reader)
        if isinstance(inferred, AmbiguousCards):
            _log_reply(reply, f"ambiguous: questions {inferred.question_ids} share this thread")
            return inferred if owner_reply else None
        if isinstance(inferred, UnmatchedCardThread):
            _log_reply(reply, f"ignored: {inferred.reason}")
            return inferred if owner_reply else None
        question, key = inferred
    if question is None or not is_question_addressee(question, reply.user_id, backend):
        if question is None:
            no_card = "no pending card in this thread" if reply.thread_ts else "no pending card"
            _log_reply(reply, f"ignored: {key or no_card}")
        else:
            _log_reply(reply, "rejected: author is not the addressee")
        return None
    return _bind_found(reply, question=question, body=body, reader=reader, key=key)


def _bind_found(
    reply: PendingChatInjection, *, question: DeferredQuestion, body: str, reader: InboundReader, key: str
) -> BoundAnswer | UnmatchedCardThread | None:
    if is_action_question(question):
        _log_reply(reply, "action-card discussion requested")
        return UnmatchedCardThread(CardThreadStop.OPTIONS_ONLY, question.pk) if reply.thread_ts else None
    answer = resolve_answer(body, question, reader=reader)
    if answer is None:
        if reply.thread_ts and question.audience == DeferredQuestion.Audience.OWNER_QUESTION:
            _log_reply(reply, "card discussion requested")
            options = tuple(str(option.get("label", "")) for option in _recorded_options(question) or [])
            return UnmatchedCardThread(CardThreadStop.OPTIONS_ONLY, question.pk, options)
        _log_reply(reply, f"ignored: {_READS_AS_QUESTION}")
        return None
    _log_reply(reply, f"bound question {question.pk}", key=key)
    return BoundAnswer(question=question, answer=answer, answered_by=reply.user_id)


def _log_reply(reply: PendingChatInjection, decision: str, *, key: str = "") -> None:
    if reply.thread_ts or reply.text.lstrip().startswith("#"):
        logger.info(
            "slack reply %s in %s thread %s: %s%s",
            reply.slack_ts,
            reply.channel,
            reply.thread_ts,
            decision,
            f" key={key}" if key else "",
        )


def log_coalesced_replies(
    rows: list[PendingChatInjection], result: BoundAnswer | AmbiguousCards | UnmatchedCardThread | None
) -> None:
    if isinstance(result, BoundAnswer):
        decision = f"bound question {result.question.pk} as part of a coalesced turn"
    elif isinstance(result, AmbiguousCards):
        decision = f"ambiguous: questions {result.question_ids} share this thread"
    elif isinstance(result, UnmatchedCardThread):
        decision = f"rejected: {result.reason}"
    else:
        decision = "ignored: this coalesced turn did not select a card"
    for row in rows[1:]:
        _log_reply(row, decision)


def apply_bound_answer(
    bound: BoundAnswer,
    *,
    backend: "MessagingBackend | None" = None,
    on_answer: Callable[[DeferredQuestion], object] | None = None,
    resume_task: bool = True,
) -> bool:
    """Resolve the bound question and close its posted card; optionally resume its parked task.

    ``False`` means a concurrent answer won the single-use CAS, which the caller
    treats as "this reply resolved nothing" and rolls its own claim back.
    """
    if is_action_question(bound.question) and not bound.from_click:
        return False
    with transaction.atomic():
        applied = bound.question.apply_answer(bound.answer, resolved_via=bound.via)
        if applied is not None and bound.answered_by:
            DeferredQuestionAudit.objects.create(
                question=applied,
                action="answered",
                answer_text=applied.answer_text,
                resolver_id=f"{bound.via}:{bound.answered_by}",
                note="button" if bound.from_click else "",
            )
        if applied is not None and resume_task:
            from teatree.core.models.task_handoff import resume_on_answer  # noqa: PLC0415 — lazy ORM import

            resume_on_answer(applied, applied.parked_task)
    if applied is None:
        return False
    if on_answer is not None:
        on_answer(applied)
    if backend is not None and applied.audience == DeferredQuestion.Audience.OWNER_QUESTION:
        replace_root(applied, closed_message(applied, answered_line(applied)), backend)
    return True


def answer_from_click(
    payload: dict,
    *,
    backend: "MessagingBackend",
    on_answer: Callable[[DeferredQuestion], object] | None = None,
) -> bool:
    """Record the addressee's tapped option and close the card; ``True`` only when this tap answered it."""
    tap = _parse_tap(payload)
    if tap is None:
        return False
    question = (
        DeferredQuestion.objects.filter(
            slack_channel=tap.channel,
            slack_ts__in=(tap.message_ts, tap.root_ts),
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
        )
        .order_by("-slack_ts")
        .first()
    )
    if question is None:
        return False
    if not is_question_addressee(question, tap.user_id, backend):
        logger.warning("Refused a tap on the card at message %s: not the owner or ticket addressee", tap.root_ts)
        return False
    if question.is_pending:
        label = _tapped_label(question, tap)
        if label is None:
            return False
        if apply_bound_answer(
            BoundAnswer(question=question, answer=label, answered_by=tap.user_id, from_click=True),
            backend=backend,
            on_answer=on_answer,
        ):
            return True
        question.refresh_from_db()
    closing = NO_LONGER_NEEDED if question.dismissed_at else f"{ALREADY_ANSWERED}{question.answer_text}"
    replace_root(question, closed_message(question, closing), backend)
    return False


def _owner_free_text_answer(text: str) -> str | None:
    explicit = _EXPLICIT_ANSWER_RE.match(text)
    if explicit is not None:
        return explicit.group(1)
    return text if text.strip().casefold() in {"yes", "no", "approve", "decline"} else None


def resolve_answer(text: str, question: DeferredQuestion, *, reader: InboundReader) -> str | None:
    """Map a reply body to the answer to apply, or ``None`` when it is not one.

    A digit ``N`` requires the question's ``options_hash`` to still match the
    live option set: a mismatch returns ``None`` (stale — no wrong-label
    application) so the reply is left for the ordinary DM path; a matching hash
    with ``N`` in range maps to ``options[N-1].label``. An out-of-range digit
    is applied as typed only for non-owner questions without recorded options.

    A free-text body used to be applied as typed, which meant a message that was
    itself a QUESTION got consumed as the answer to the pending one, claimed, and
    reacted ✅ — the owner's question answered by nobody and marked handled. So a
    non-digit body on other questions is READ first, and an interrogative one
    returns ``None``: it is left to the reactive cycle, which answers it or dispatches it. Owner
    option cards accept only recorded labels without a model read; an optionless
    owner card requires ``Answer:`` or a short explicit decision word. The asymmetry
    is deliberate — mistaking a question for an answer destroys the question,
    while mistaking an answer for a question costs one extra round trip.
    """
    if not text.strip() or is_action_question(question):
        return None
    if question.audience == DeferredQuestion.Audience.OWNER_QUESTION and not _recorded_options(question):
        return _owner_free_text_answer(text)
    answer: str | None
    match = _DIGIT_RE.match(text)
    if match is None:
        options = _recorded_options(question) or []
        for option in options:
            label = str(option.get("label", ""))
            if label and label.casefold() == text.strip().casefold():
                return label
        if (
            not (options and question.audience == DeferredQuestion.Audience.OWNER_QUESTION)
            and reader(text).intent is InboundIntent.QUESTION
        ):
            return None
        answer = text
    else:
        options = _live_options(question)
        if options is None:
            return None
        index = int(match.group(1))
        answer = str(options[index - 1].get("label", "")) or text if 1 <= index <= len(options) else text
    if question.audience == DeferredQuestion.Audience.OWNER_QUESTION and _recorded_options(question):
        labels = {str(option.get("label", "")) for option in _recorded_options(question) or []}
        if answer not in labels:
            answer = None
    return answer


@dataclass(frozen=True, slots=True)
class _Tap:
    user_id: str
    channel: str
    root_ts: str
    message_ts: str
    index: int
    label: str


def _parse_tap(payload: dict) -> _Tap | None:
    """The owner-question button *payload* reports, or ``None`` for any other interaction."""
    action = next(
        (a for a in payload.get("actions") or [] if str(a.get("action_id", "")).startswith(OPTION_ACTION_PREFIX)), None
    )
    message = payload.get("message") or {}
    value = str(action.get("value", "")) if action else ""
    if action is None or not value.isdigit():
        return None
    return _Tap(
        user_id=str((payload.get("user") or {}).get("id", "")),
        channel=str((payload.get("channel") or {}).get("id", "")),
        root_ts=str(message.get("thread_ts") or message.get("ts") or ""),
        message_ts=str(message.get("ts") or ""),
        index=int(value),
        label=str((action.get("text") or {}).get("text", "")),
    )


def _tapped_label(question: DeferredQuestion, tap: _Tap) -> str | None:
    """The label of the tapped option, or ``None`` when the card no longer offers it."""
    options = _recorded_options(question)  # not _live_options: a producer may keep its own key in options_hash
    if options is None or not (1 <= tap.index <= len(options)):
        return None
    label = str(options[tap.index - 1].get("label", ""))
    return label if label and label == tap.label else None


def _addressed(text: str) -> tuple[int, str] | None:
    """The ``(question id, answer body)`` of a ``#<id>``-prefixed reply, else ``None``.

    An addressed reply that names a stale or unknown id binds nothing rather
    than falling through — the owner said which question they meant, and
    inferring a different one from a typo is the wrong-apply this ladder exists
    to remove.
    """
    match = _ID_PREFIX_RE.match(text)
    return None if match is None else (int(match.group(1)), (match.group(2) or "").strip())


def _pending_by_id(question_id: int, *, channel: str) -> DeferredQuestion | None:
    return DeferredQuestion.objects.filter(
        pk=question_id,
        slack_channel=channel,
        answered_at__isnull=True,
        dismissed_at__isnull=True,
    ).first()


def _inferred_cards(
    reply: PendingChatInjection, *, backend: "MessagingBackend", body: str, reader: InboundReader
) -> tuple[DeferredQuestion | None, str] | AmbiguousCards | UnmatchedCardThread:
    if not reply.thread_ts:
        return DeferredQuestion.sole_for_reply(channel=reply.channel, after_ts=reply.slack_ts), "sole"
    return _thread_cards(reply, backend=backend, body=body, reader=reader)


def cards_in_thread(
    *, channel: str, thread_ts: str, before_ts: str, backend: "MessagingBackend"
) -> tuple[list[DeferredQuestion], str]:
    """Every card, pending or closed, whose own ts belongs to the reply's thread, and the key that matched."""
    posted = list(DeferredQuestion.posted_since(channel=channel, root_ts=thread_ts, before_ts=before_ts))
    if len(posted) == 1 and posted[0].slack_ts == thread_ts:
        return posted, "root"
    if not posted:
        return [], ""
    messages = backend.fetch_thread_replies(channel=channel, thread_ts=thread_ts)
    members = {thread_ts, *(str(message.get("ts", "")) for message in messages)}
    return [card for card in posted if card.slack_ts in members], "thread-member"


def _thread_cards(
    reply: PendingChatInjection, *, backend: "MessagingBackend", body: str, reader: InboundReader
) -> tuple[DeferredQuestion | None, str] | AmbiguousCards | UnmatchedCardThread:
    ticket_thread = TicketDmThread.objects.filter(channel=reply.channel, root_ts=reply.thread_ts).first()
    if ticket_thread is not None:
        latest = (
            TicketDmPost.objects.filter(
                thread=ticket_thread, generation=ticket_thread.generation, slack_ts__lt=reply.slack_ts
            )
            .order_by("-slack_ts", "-pk")
            .first()
        )
        question = (
            DeferredQuestion.objects.filter(pk=latest.question_id).first() if latest and latest.question_id else None
        )
        return (question, "ticket-newest") if question is not None and question.is_pending else (None, "")
    return _ordinary_thread_cards(reply, backend=backend, body=body, reader=reader)


def _ordinary_thread_cards(
    reply: PendingChatInjection, *, backend: "MessagingBackend", body: str, reader: InboundReader
) -> tuple[DeferredQuestion | None, str] | AmbiguousCards | UnmatchedCardThread:
    matched, key = cards_in_thread(
        channel=reply.channel, thread_ts=reply.thread_ts, before_ts=reply.slack_ts, backend=backend
    )
    all_pending = [card for card in matched if card.is_pending]
    pending = [card for card in all_pending if not is_action_question(card)]
    if len(all_pending) > 1:
        if not pending:
            return AmbiguousCards(tuple(card.pk for card in all_pending))
        if resolve_answer(body, pending[0], reader=reader) is None:
            return None, _READS_AS_QUESTION
        if len(pending) > 1:
            return AmbiguousCards(tuple(card.pk for card in pending))
    if pending:
        return pending[0], key
    if len(all_pending) == 1:
        return UnmatchedCardThread(CardThreadStop.OPTIONS_ONLY, all_pending[0].pk)
    return UnmatchedCardThread(CardThreadStop.CLOSED) if matched else (None, "")


def _recorded_options(question: DeferredQuestion) -> list[dict] | None:
    if not question.options_json:
        return None
    try:
        options = json.loads(question.options_json)
    except (ValueError, TypeError):
        return None
    return options if isinstance(options, list) else None


def _live_options(question: DeferredQuestion) -> list[dict] | None:
    """The recorded options when ``options_hash`` still matches, else ``None``.

    ``None`` means a digit reply cannot be safely mapped to a label (the
    option set the digit referred to has changed); the caller treats that
    digit as a stale verbatim body rather than risk a wrong-label apply.
    """
    options = _recorded_options(question)
    return options if options is not None and options_digest(options) == question.options_hash else None


__all__ = [
    "AmbiguousCards",
    "BoundAnswer",
    "CardThreadStop",
    "UnmatchedCardThread",
    "answer_from_click",
    "apply_bound_answer",
    "bind_reply",
    "cards_in_thread",
    "configured_owner_id",
    "log_coalesced_replies",
    "resolve_answer",
]
