"""Deliver simple Slack answers with claim and read-back confirmation."""

from collections.abc import Callable

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import PendingChatInjection, ThreadBinding
from teatree.core.owner_dm.ticket_dm_delivery import record_ticket_thread_post
from teatree.loop.slack_answer.coalescing import _Unit
from teatree.loop.slack_answer.session_routing import _mark_unit_loop_replied, _unmark_unit_loop_replied
from teatree.loop.slack_answer.simple_answer import (
    NEEDS_WORK_SENTINEL,
    build_simple_answer,
    is_status_question,
    ticket_status_answer,
)
from teatree.loop.slack_answer.thread_readback import bot_reply_match_in_thread, resolve_thread_root


def _handle_simple(
    backend: MessagingBackend,
    unit: _Unit,
    binding: ThreadBinding | None = None,
    *,
    on_done: Callable[[MessagingBackend, _Unit], None],
) -> str:
    """Claim, post, read back and stamp one simple answer; retry when delivery is unconfirmed."""
    answer = (
        ticket_status_answer(binding.ticket)
        if binding is not None and not binding.ticket.is_conversation() and is_status_question(unit.text)
        else build_simple_answer(unit.lead)
    )
    if answer is None or answer == NEEDS_WORK_SENTINEL:
        return "needs_work"
    if not _mark_unit_loop_replied(unit, PendingChatInjection.AnswerKind.SIMPLE):
        return "retry"
    try:
        thread_root = resolve_thread_root(backend, channel=unit.channel, ts=unit.slack_ts)
        floor = unit.rows[-1].slack_ts
        matched_ts = bot_reply_match_in_thread(backend, channel=unit.channel, thread_root=thread_root, after_ts=floor)
        if matched_ts is None:
            post = backend.post_reply(channel=unit.channel, ts=unit.slack_ts, text=answer)
            posted_ts = post.get("ts")
            matched_ts = (
                bot_reply_match_in_thread(
                    backend, channel=unit.channel, thread_root=thread_root, after_ts=floor, expected_ts=posted_ts
                )
                if post.get("ok") is True and isinstance(posted_ts, str)
                else None
            )
            if matched_ts is None:
                _unmark_unit_loop_replied(unit)
                return "retry"
        record_ticket_thread_post(unit.channel, thread_root, matched_ts)
    except Exception:
        _unmark_unit_loop_replied(unit)
        raise
    for row in unit.rows:
        row.observe_confirmed_loop_reply()
    on_done(backend, unit)
    return "simple"
