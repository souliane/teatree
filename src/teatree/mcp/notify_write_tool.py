"""The MCP write tool for the audited bot→user DM: the same seam as ``t3 <overlay> notify dm``."""

from typing import Any

from asgiref.sync import sync_to_async
from mcp.server.mcpserver.exceptions import ToolError

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.owner_decision import OWNER_QUESTION_ROUTE
from teatree.core.notify import NotifyKind, notify_user_outcome
from teatree.core.notify_types import NotifyOptions


async def notify_user(text: str, *, kind: str = "info", idempotency_key: str) -> dict[str, Any]:
    """Send a bot→user DM through the audited notify egress.

    Wraps :func:`teatree.core.notify.notify_user_outcome` — the single notification
    egress with the send-proxy, the BotPing idempotency ledger, and the own-DM
    never-lockout carve-out. Pass a stable ``idempotency_key`` so a retry under
    the same key is a no-op rather than a duplicate DM.

    ``kind`` is ``answer`` or ``info``. ``question`` is refused with
    :data:`~teatree.core.modelkit.owner_decision.OWNER_QUESTION_ROUTE`: an owner
    question is recorded with its decision and checked facts, never sent as a DM.
    An unknown kind is refused with the valid set named, never a bare enum traceback.

    A non-delivery ALWAYS names itself: ``reason`` is the machine-readable
    :class:`~teatree.core.notify.NotifyReason` (``no_messaging_backend``,
    ``no_user_id``, ``delivery_failed``, …) and ``detail``
    the human sentence. A bare ``sent=false`` with no reason is what let five
    completed reviews go unannounced for a day — the caller could not tell one
    non-delivery from another, so nothing escalated.

    Every call here is a DIRECTLY REQUESTED DM: this surface is reachable only from
    an agent's deliberate tool call, never from a recurring alarm site, so the
    push/pull registry does not withhold it. Without that, a one-off message about
    one merge request read as an unregistered alarm and was recorded rather than
    delivered. An alarm still earns its DM by registering in
    :data:`~teatree.core.modelkit.dm_channel_policy.PUSH_SIGNALS`.
    """
    if kind == NotifyKind.QUESTION:
        raise ToolError(OWNER_QUESTION_ROUTE)
    sendable = {m.value: m for m in NotifyKind if m not in {NotifyKind.QUESTION, NotifyKind.RELAY}}
    if (kind_value := sendable.get(kind)) is None:
        msg = f"invalid kind {kind!r} — valid kinds: {' | '.join(sendable)}. {OWNER_QUESTION_ROUTE}"
        raise ToolError(msg)
    outcome = await sync_to_async(
        lambda: notify_user_outcome(
            text,
            kind=kind_value,
            idempotency_key=idempotency_key,
            audience=NotifyAudience.OWNER_DELIVERY,
            options=NotifyOptions(requested_push=True),
        ),
        thread_sensitive=True,
    )()
    return {
        "ok": outcome.sent,
        "sent": outcome.sent,
        "idempotency_key": idempotency_key,
        "reason": outcome.reason.value,
        "detail": outcome.detail,
    }
