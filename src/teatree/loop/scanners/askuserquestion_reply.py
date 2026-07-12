"""Bind a Slack reply to its live ``DeferredQuestion`` and apply it (#1174).

The second leg of the Slack→Claude bridge. The PreToolUse capture arm
records each loop-driven ``AskUserQuestion`` as a mirror-linked
``DeferredQuestion`` and posts it to the user's DM; the user replies on
Slack and the reply lands as a ``PendingChatInjection`` row. This scanner
matches each unconsumed reply to the currently-live question for that DM
channel and applies it:

- a digit body ``N`` with ``1 ≤ N ≤ len(options)`` maps to
``options[N-1].label`` only when the row's ``options_hash`` still matches
the options the digit refers to (a changed option set leaves the reply
stale, no wrong label); any other body is applied verbatim.
- ``apply_answer(resolved_via="slack")`` is the single-use CAS that
resolves exactly the live row.
- the reply's ``loop_replied_at`` is claimed with kind ``question_reply``
so the reactive Slack-answer cycle does NOT spawn an answerer — but
``answered_at`` is left untouched (#1063 turn-end gate stays decoupled).
- a ✅ reaction goes out through :class:`OnBehalfSlackEgress` (the reply is
in the user's own DM, so the self-DM short-circuit posts it ungated),
verify-by-readback before the claim is kept — a react/readback failure
rolls the claim back so the unit retries next cycle.

A reply with no live question is left untouched for the ordinary DM drain
/ reactive cycle — never forced into a question result.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import PendingChatInjection
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

_BATCH = 20
_ACK_EMOJI = "white_check_mark"
_DIGIT_RE = re.compile(r"^\s*([1-9][0-9]*)\s*$")


@dataclass(slots=True)
class AskUserQuestionReplyScanner:
    """Apply each Slack reply to its live ``DeferredQuestion`` (#1174).

    *overlay* tags which queue to drain (``""`` drains every overlay's
    queue for the v1 single-overlay path). The scanner produces no
    statusline signal — the applied answer surfaces via the
    ``handle_inject_pending_questions`` UserPromptSubmit drain, not the
    statusline — so :meth:`scan` returns an empty signal list.
    """

    backend: MessagingBackend
    overlay: str = ""
    name: str = "askuserquestion_reply"

    def scan(self) -> list[ScanSignal]:
        egress = OnBehalfSlackEgress(self.backend)
        for reply in list(PendingChatInjection.loop_unreplied(overlay=self.overlay)[:_BATCH]):
            try:
                self._apply_one(reply, egress)
            except Exception:
                logger.exception("AskUserQuestionReplyScanner failed on reply %s", reply.pk)
        return []

    def _apply_one(self, reply: PendingChatInjection, egress: OnBehalfSlackEgress) -> None:
        question = DeferredQuestion.live_for_reply(channel=reply.channel, after_ts=reply.slack_ts)
        if question is None:
            return
        answer = _resolve_answer(reply.text, question)
        if answer is None:
            return
        if not reply.mark_loop_replied(PendingChatInjection.AnswerKind.QUESTION_REPLY):
            return
        if not self._react_ack(reply, egress):
            # React/readback failed — leave the question pending and release
            # the reply so the whole unit retries next cycle (the answer is
            # never recorded against a reply the user never saw acknowledged).
            reply.unmark_loop_replied()
            return
        applied = question.apply_answer(answer, resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        if applied is None:
            reply.unmark_loop_replied()
            return
        if applied.parked_task_id:  # ty: ignore[unresolved-attribute]
            from teatree.core.models.task_handoff import schedule_headless_resume  # noqa: PLC0415 — lazy ORM import

            schedule_headless_resume(applied.parked_task, answer=answer)

    def _react_ack(self, reply: PendingChatInjection, egress: OnBehalfSlackEgress) -> bool:
        try:
            egress.react(
                channel=reply.channel,
                ts=reply.slack_ts,
                emoji=_ACK_EMOJI,
                target=reply.slack_ts,
                action="askuserquestion_reply_ack",
            )
        except OnBehalfPostBlockedError:
            return False
        except Exception:  # noqa: BLE001 — never break a cycle on a react raise
            return False
        return bool(self.backend.get_permalink(channel=reply.channel, ts=reply.slack_ts))


def _resolve_answer(text: str, question: DeferredQuestion) -> str | None:
    """Map a reply body to the answer to apply, or ``None`` when it is stale.

    A non-digit body is applied verbatim. A digit ``N`` requires the
    question's ``options_hash`` to still match the live option set: a
    mismatch returns ``None`` (stale — no wrong-label application) so the
    reply is left for the ordinary DM path; a matching hash with ``N`` in
    range maps to ``options[N-1].label``, and an out-of-range ``N`` is
    applied verbatim.
    """
    match = _DIGIT_RE.match(text)
    if match is None:
        return text
    options = _live_options(question)
    if options is None:
        return None
    index = int(match.group(1))
    if not (1 <= index <= len(options)):
        return text
    return str(options[index - 1].get("label", "")) or text


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


__all__ = ["AskUserQuestionReplyScanner"]
