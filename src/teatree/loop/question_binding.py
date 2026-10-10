"""Which queued question a Slack reply answers — one binder, both consumers.

Two independent consumers drain the same ``PendingChatInjection.loop_unreplied()``
queue: the tick-cadence ``AskUserQuestionReplyScanner`` and the event-driven
``run_slack_answer_cycle`` (an inbound-event wake, ~1s). The cycle wins nearly
every race, and it knew nothing about :class:`DeferredQuestion` — so it stamped
``loop_replied_at``, reacted, and the binder never saw the row. The owner's
answer was acknowledged and dropped. Both consumers now bind through this
module, so the reply→question join cannot drift between them.

The ladder, strongest evidence first:

(a) an explicit ``#<id>`` prefix — the format the backlog digest already
instructs the owner to reply in, and which until now nothing parsed — naming a
question mirrored on the reply's own DM;
(b) the reply's ``thread_ts`` — an exact join onto the question's mirror ts;
(c) a top-level reply when exactly ONE live question is mirrored on the channel.

More than one candidate and no thread or id binds NOTHING. The newest-pending-
wins pick it replaces silently answered the wrong row: with 149 questions
mirrored into a single DM, a reply to a three-day-old question resolved
whichever had been posted last, and ✅-acked the owner for it.

Every rung binds only a reply whose recorded author is the owner's Slack user id;
any other reply, or an unknown owner id, binds nothing and is left for the DM path.

A tap on an option button is the strongest evidence of all: :func:`answer_from_click` joins the
tapped message onto its question by channel and root ``ts``, and records the option's label.
Every applied answer, typed or tapped, closes the posted card in place.
"""

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models import PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.question_text import options_digest
from teatree.core.owner_question_message import (
    ALREADY_ANSWERED,
    NO_LONGER_NEEDED,
    answered_line,
    closed_message,
    replace_root,
)
from teatree.loop.inbound_reading import InboundIntent, InboundReader

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

# The digest's instructed form, ``#<id> <your answer>``. At least one separator
# is required so ``#12abc`` is not read as question 12 answered "abc".
_ID_PREFIX_RE = re.compile(r"^\s*#(\d+)(?:[\s:.,\-]+(.*))?$", re.DOTALL)
_DIGIT_RE = re.compile(r"^\s*([1-9][0-9]*)\s*$")
OPTION_ACTION_PREFIX = "owner-question-option-"


@dataclass(frozen=True, slots=True)
class BoundAnswer:
    """A reply matched to the question it answers, with the text to apply."""

    question: DeferredQuestion
    answer: str


def configured_owner_id(backend: object) -> str:
    return str(getattr(backend, "user_id", "") or "")


def bind_reply(
    reply: PendingChatInjection, *, reader: InboundReader, owner_user_id: str, text: str = ""
) -> BoundAnswer | None:
    """The question *reply* answers and the answer to apply, or ``None``.

    *text* overrides the row body for a caller that coalesces several rows into
    one logical turn. See the module docstring for the binding ladder.
    """
    if not owner_user_id or reply.user_id != owner_user_id:
        return None
    body = text or reply.text
    addressed = _addressed(body)
    if addressed is not None:
        question_id, body = addressed
        question = _pending_by_id(question_id, channel=reply.channel)
    else:
        question = _inferred_question(reply)
    if question is None:
        return None
    answer = resolve_answer(body, question, reader=reader)
    return None if answer is None else BoundAnswer(question=question, answer=answer)


def apply_bound_answer(bound: BoundAnswer, *, backend: "MessagingBackend | None" = None) -> bool:
    """Resolve the bound question, resume its parked task and close its posted card; ``True`` when applied.

    ``False`` means a concurrent answer won the single-use CAS, which the caller
    treats as "this reply resolved nothing" and rolls its own claim back.
    """
    applied = bound.question.apply_answer(bound.answer, resolved_via=DeferredQuestion.ResolvedVia.SLACK)
    if applied is None:
        return False
    from teatree.core.models.task_handoff import resume_on_answer  # noqa: PLC0415 — lazy ORM import

    resume_on_answer(applied, applied.parked_task)
    if backend is not None and applied.audience == DeferredQuestion.Audience.OWNER_QUESTION:
        replace_root(applied, closed_message(applied, answered_line(applied)), backend)
    return True


def answer_from_click(payload: dict, *, backend: "MessagingBackend") -> bool:
    """Record the option the owner tapped and close the card; ``True`` only when this tap answered it."""
    tap = _parse_tap(payload)
    if tap is None or tap.user_id != configured_owner_id(backend):
        return False
    question = DeferredQuestion.objects.filter(
        slack_channel=tap.channel, slack_ts=tap.root_ts, audience=DeferredQuestion.Audience.OWNER_QUESTION
    ).first()
    if question is None:
        return False
    if question.is_pending:
        label = _tapped_label(question, tap)
        if label is None:
            return False
        if apply_bound_answer(BoundAnswer(question=question, answer=label), backend=backend):
            return True
        question.refresh_from_db()
    closing = NO_LONGER_NEEDED if question.dismissed_at else f"{ALREADY_ANSWERED}{question.answer_text}"
    replace_root(question, closed_message(question, closing), backend)
    return False


def resolve_answer(text: str, question: DeferredQuestion, *, reader: InboundReader) -> str | None:
    """Map a reply body to the answer to apply, or ``None`` when it is not one.

    A digit ``N`` requires the question's ``options_hash`` to still match the
    live option set: a mismatch returns ``None`` (stale — no wrong-label
    application) so the reply is left for the ordinary DM path; a matching hash
    with ``N`` in range maps to ``options[N-1].label``, and an out-of-range ``N``
    is applied verbatim. A digit is unambiguous, so it never costs a model turn.

    A free-text body used to be applied verbatim, which meant a message that was
    itself a QUESTION got consumed as the answer to the pending one, claimed, and
    reacted ✅ — the owner's question answered by nobody and marked handled. So a
    non-digit body is READ first, and an interrogative one returns ``None``: it is
    left to the reactive cycle, which answers it or dispatches it. The asymmetry
    is deliberate — mistaking a question for an answer destroys the question,
    while mistaking an answer for a question costs one extra round trip.
    """
    if not text.strip():
        return None
    match = _DIGIT_RE.match(text)
    if match is None:
        return None if reader(text).intent is InboundIntent.QUESTION else text
    options = _live_options(question)
    if options is None:
        return None
    index = int(match.group(1))
    if not (1 <= index <= len(options)):
        return text
    return str(options[index - 1].get("label", "")) or text


@dataclass(frozen=True, slots=True)
class _Tap:
    user_id: str
    channel: str
    root_ts: str
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


def _inferred_question(reply: PendingChatInjection) -> DeferredQuestion | None:
    """Rungs (b) and (c): the thread root's question, else the only live one.

    Rung (c) is reachable only from a TOP-LEVEL reply, which is what
    ``sole_for_reply`` is: with one live mirror such a reply cannot be for anything
    else. A reply carrying a ``thread_ts`` has already named its referent, so a
    ``thread_ts`` matching no mirror means it answers the info/watchdog DM it hangs
    under — not the sole open question. Falling through consumed the owner's
    INSTRUCTION as that question's answer and ✅-acked it, leaving the instruction
    with no consumer at all; unbound, it reaches the reactive cycle, which dispatches
    or files it (#4527).
    """
    threaded = DeferredQuestion.for_thread(
        channel=reply.channel,
        thread_ts=reply.thread_ts,
        after_ts=reply.slack_ts,
    )
    if threaded is not None or reply.thread_ts:
        return threaded
    return DeferredQuestion.sole_for_reply(channel=reply.channel, after_ts=reply.slack_ts)


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
    "BoundAnswer",
    "answer_from_click",
    "apply_bound_answer",
    "bind_reply",
    "configured_owner_id",
    "resolve_answer",
]
