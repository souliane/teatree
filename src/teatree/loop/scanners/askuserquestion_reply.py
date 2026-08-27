"""Bind a Slack reply to its queued ``DeferredQuestion`` and apply it (#1174).

The second leg of the Slack→Claude bridge. The PreToolUse capture arm
records each loop-driven ``AskUserQuestion`` as a mirror-linked
``DeferredQuestion`` and posts it to the user's DM; the user replies on
Slack and the reply lands as a ``PendingChatInjection`` row. This scanner
drains unconsumed replies and applies each to the question it answers:

- :func:`~teatree.loop.question_binding.bind_reply` decides which question a
reply answers. It is the ladder shared with the reactive Slack-answer cycle,
which drains the same queue and used to win the race knowing nothing about
questions at all.
- ``apply_answer(resolved_via="slack")`` is the single-use CAS that
resolves exactly the bound row.
- the reply's ``loop_replied_at`` is claimed with kind ``question_reply``
so the reactive Slack-answer cycle does NOT spawn an answerer — but
``answered_at`` is left untouched (#1063 turn-end gate stays decoupled).
- a ✅ reaction goes out through :class:`OnBehalfSlackEgress` (the reply is
in the user's own DM, so the self-DM short-circuit posts it ungated),
verify-by-readback before the claim is kept — a react/readback failure
rolls the claim back so the unit retries next cycle.

A reply that binds no question is left untouched for the ordinary DM drain
/ reactive cycle — never forced into a question result.
"""

import logging
from dataclasses import dataclass

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import PendingChatInjection
from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.loop.inbound_reading import InboundReader, read_inbound
from teatree.loop.question_binding import apply_bound_answer, bind_reply
from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

_BATCH = 20
_ACK_EMOJI = "white_check_mark"


@dataclass(slots=True)
class AskUserQuestionReplyScanner:
    """Apply each Slack reply to the ``DeferredQuestion`` it answers (#1174).

    *overlay* tags which queue to drain (``""`` drains every overlay's
    queue for the v1 single-overlay path). The scanner produces no
    statusline signal — the applied answer surfaces via the
    ``handle_inject_pending_questions`` UserPromptSubmit drain, not the
    statusline — so :meth:`scan` returns an empty signal list.
    """

    backend: MessagingBackend
    overlay: str = ""
    name: str = "askuserquestion_reply"
    reader: "InboundReader | None" = None

    def scan(self) -> list[ScanSignal]:
        egress = OnBehalfSlackEgress(self.backend)
        for reply in list(PendingChatInjection.loop_unreplied(overlay=self.overlay)[:_BATCH]):
            try:
                self._apply_one(reply, egress)
            except Exception:
                logger.exception("AskUserQuestionReplyScanner failed on reply %s", reply.pk)
        return []

    def _apply_one(self, reply: PendingChatInjection, egress: OnBehalfSlackEgress) -> None:
        bound = bind_reply(reply, reader=self.reader or read_inbound)
        if bound is None:
            return
        if not reply.mark_loop_replied(PendingChatInjection.AnswerKind.QUESTION_REPLY):
            return
        if not self._react_ack(reply, egress):
            # React/readback failed — leave the question pending and release
            # the reply so the whole unit retries next cycle (the answer is
            # never recorded against a reply the user never saw acknowledged).
            reply.unmark_loop_replied()
            return
        try:
            applied = apply_bound_answer(bound)
        except Exception:
            # Nothing was applied, so the claim must go back — otherwise the
            # reply is consumed forever by a failure that never recorded it.
            reply.unmark_loop_replied()
            raise
        if not applied:
            reply.unmark_loop_replied()

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


__all__ = ["AskUserQuestionReplyScanner"]
