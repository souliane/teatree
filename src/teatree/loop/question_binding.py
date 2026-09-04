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
instructs the owner to reply in, and which until now nothing parsed;
(b) the reply's ``thread_ts`` — an exact join onto the question's mirror ts;
(c) a top-level reply when exactly ONE live question is mirrored on the channel.

More than one candidate and no thread or id binds NOTHING. The newest-pending-
wins pick it replaces silently answered the wrong row: with 149 questions
mirrored into a single DM, a reply to a three-day-old question resolved
whichever had been posted last, and ✅-acked the owner for it.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from teatree.core.models import PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.inbound_reading import InboundIntent, InboundReader

# The digest's instructed form, ``#<id> <your answer>``. At least one separator
# is required so ``#12abc`` is not read as question 12 answered "abc".
_ID_PREFIX_RE = re.compile(r"^\s*#(\d+)(?:[\s:.,\-]+(.*))?$", re.DOTALL)
_DIGIT_RE = re.compile(r"^\s*([1-9][0-9]*)\s*$")


@dataclass(frozen=True, slots=True)
class BoundAnswer:
    """A reply matched to the question it answers, with the text to apply."""

    question: DeferredQuestion
    answer: str


def bind_reply(reply: PendingChatInjection, *, reader: InboundReader, text: str = "") -> BoundAnswer | None:
    """The question *reply* answers and the answer to apply, or ``None``.

    *text* overrides the row body for a caller that coalesces several rows into
    one logical turn. See the module docstring for the binding ladder.
    """
    body = text or reply.text
    addressed = _addressed(body)
    if addressed is not None:
        question_id, body = addressed
        question = _pending_by_id(question_id)
    else:
        question = _inferred_question(reply)
    if question is None:
        return None
    answer = resolve_answer(body, question, reader=reader)
    return None if answer is None else BoundAnswer(question=question, answer=answer)


def apply_bound_answer(bound: BoundAnswer) -> bool:
    """Resolve the bound question and resume any parked task; ``True`` when applied.

    ``False`` means a concurrent answer won the single-use CAS, which the caller
    treats as "this reply resolved nothing" and rolls its own claim back.
    """
    applied = bound.question.apply_answer(bound.answer, resolved_via=DeferredQuestion.ResolvedVia.SLACK)
    if applied is None:
        return False
    parked = applied.parked_task
    if parked is not None:
        from teatree.core.models.task_handoff import schedule_resume  # noqa: PLC0415 — lazy ORM import

        schedule_resume(parked, answer=bound.answer)
    return True


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


def _addressed(text: str) -> tuple[int, str] | None:
    """The ``(question id, answer body)`` of a ``#<id>``-prefixed reply, else ``None``.

    An addressed reply that names a stale or unknown id binds nothing rather
    than falling through — the owner said which question they meant, and
    inferring a different one from a typo is the wrong-apply this ladder exists
    to remove.
    """
    match = _ID_PREFIX_RE.match(text)
    return None if match is None else (int(match.group(1)), (match.group(2) or "").strip())


def _pending_by_id(question_id: int) -> DeferredQuestion | None:
    return DeferredQuestion.objects.filter(
        pk=question_id,
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


def _live_options(question: DeferredQuestion) -> list[dict] | None:
    """The recorded options when ``options_hash`` still matches, else ``None``.

    ``None`` means a digit reply cannot be safely mapped to a label (the
    option set the digit referred to has changed); the caller treats that
    digit as a stale verbatim body rather than risk a wrong-label apply.
    """
    if not question.options_json:
        return None
    try:
        options = json.loads(question.options_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(options, list):
        return None
    blob = json.dumps(options, sort_keys=True, ensure_ascii=False)
    if hashlib.sha256(blob.encode("utf-8")).hexdigest() != question.options_hash:
        return None
    return options


__all__ = ["BoundAnswer", "apply_bound_answer", "bind_reply", "resolve_answer"]
