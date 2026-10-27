"""After-receipt notifier for colleague-visible on-behalf posts (#949).

The companion of the *pre*-gate :mod:`teatree.core.on_behalf_gate_recorded`
(which decides whether a post may publish). This module fires *after* a
colleague-visible post has already published successfully — it DMs the
user the destination, a clickable artifact link, and a one-line summary
so every post made under their identity is visible to them.

Distinct concern from the pre-gate (kept in its own module so the
pre-gate stays pure): the pre-gate may BLOCK; this never can — the post
already happened, the caller already published. So the failure model is
**record-and-proceed**: attempt the notify, durably record its outcome
via :func:`teatree.core.notify.notify_user` (which never raises into the
caller and writes a ``BotPing`` FAILED/NOOP + ``OutboundClaim`` row on
non-delivery so the audit scanner re-DMs on drift), then return. Never
raise, never fail or roll back the post — a colleague comment cannot be
un-posted.

The receipt runs after every colleague-visible on-behalf post.

Depends only on :mod:`teatree.core.notify`, an already legal
``teatree.core`` edge, so no new tach edge.
"""

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.core.gates.forge_write_guard import ForgeWriteGuard
from teatree.core.modelkit.notify_policy import NotifyAudience

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from teatree.core.models.bot_ping import BotPing


def _post_receipt_to_channel(channel: str, key: str, text: str, artifact_url: str) -> bool:
    from teatree.core.notify import resolve_owner_dm_backend  # noqa: PLC0415 — deferred: call-time import, kept lazy
    from teatree.core.on_behalf_egress import OnBehalfSlackEgress  # noqa: PLC0415 — avoids a receipt import cycle

    try:
        ping_model = cast("type[BotPing]", apps.get_model("core", "BotPing"))
        backend, _ = resolve_owner_dm_backend()
        if backend is None:
            return False
        claim = ping_model.claim_delivery(key, kind="info", text=text, audience=NotifyAudience.COLLEAGUE_ACTION.value)
        if claim != "claimed":
            return True
        try:
            response = OnBehalfSlackEgress(backend).post(
                channel=channel, text=text, target=key, action="receipt", receipt=False
            )
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            ping_model.finalize_failed(key, error=str(exc))
            return False
        posted_ts = str(response.get("ts") or "") if isinstance(response, dict) else ""
        if isinstance(response, dict) and response.get("ok") and posted_ts:
            with suppress(Exception):
                ping_model.finalize_sent(key, channel_ref=channel, posted_ts=posted_ts, permalink=artifact_url)
            return True
        ping_model.finalize_failed(key, error="receipt channel did not confirm delivery")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        logger.debug("receipt channel delivery failed")
    return False


def notify_user_on_behalf_post(
    *,
    target: str,
    action: str,
    destination: str,
    artifact_url: str,
    summary: str,
) -> None:
    """DM the user that a colleague-visible post published under their identity.

    ``target``/``action``/``artifact_url`` form the idempotency key
    ``on_behalf_post:{target}:{action}:{artifact_url}`` — one DM per
    PUBLICATION, not per (target, action): a retry re-posts the same
    artifact and dedups, while a second comment on the same MR is a second
    post under the user's identity and gets its own receipt.
    ``destination`` is the human-readable place the post landed (a review
    channel, an ``org/repo!7`` ref). ``artifact_url`` is the clickable
    permalink/URL of the post; ``notify_user``'s ``maybe_linkify``
    converts the ``[label](url)`` form to Slack ``<url|label>``. A
    non-URL ``artifact_url`` (a raw Slack channel id, or an internal
    note/discussion/line id) carries NO clickable link — the receipt
    names the destination plainly rather than emitting a dead
    ``[id](id)`` link that both breaks and leaks the internal id.
    ``summary`` is the one-line description of what was posted.

    Every on-behalf publication sends this after-receipt visibility DM.

    Never raises into the caller and never fails or rolls back the post:
    ``notify_user`` already wraps every transport failure into a
    NOOP/FAILED ``BotPing`` row (the audit scanner re-DMs on drift), so
    a misconfigured Slack backend cannot break a legitimate publish.
    """
    from teatree.core.notify import NotifyKind, notify_user  # noqa: PLC0415 — deferred: call-time import, kept lazy

    if artifact_url.startswith("http"):
        short = artifact_url.rsplit("/", 1)[-1] or artifact_url
        link_line = f"[{short}]({artifact_url})\n"
    else:
        link_line = ""
    text = f"Posted under your identity to {destination}.\n{link_line}{summary}"
    key = f"on_behalf_post:{target}:{action}:{artifact_url}"
    channel = ""
    with suppress(Exception):
        guard = ForgeWriteGuard.configured()
        channel = guard.receipt_channel if guard else ""
    if channel and _post_receipt_to_channel(channel, key, text, artifact_url):
        return
    notify_user(
        text,
        kind=NotifyKind.INFO,
        idempotency_key=key,
        audience=NotifyAudience.COLLEAGUE_ACTION,
    )
