"""Delivery of the after-receipt message: the configured receipt channel first, the owner DM otherwise.

Split from :mod:`teatree.core.on_behalf_post_receipt` because posting to a channel goes through
:class:`~teatree.core.on_behalf_egress.OnBehalfSlackEgress`, which imports that module. Here both
imports are module-level; the receipt module reaches this one at call time.
"""

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, cast

from django.apps import apps

from teatree.core import notify
from teatree.core.gates.forge_write_guard import ForgeWriteGuard
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.on_behalf_egress import OnBehalfSlackEgress

if TYPE_CHECKING:
    from teatree.core.models.bot_ping import BotPing

logger = logging.getLogger(__name__)


def _post_receipt_to_channel(channel: str, key: str, text: str, artifact_url: str) -> bool:
    try:
        ping_model = cast("type[BotPing]", apps.get_model("core", "BotPing"))
        backend, _ = notify.resolve_owner_dm_backend()
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


def deliver_receipt(*, key: str, text: str, artifact_url: str) -> None:
    channel = ""
    with suppress(Exception):
        guard = ForgeWriteGuard.configured()
        channel = guard.receipt_channel if guard else ""
    if channel and _post_receipt_to_channel(channel, key, text, artifact_url):
        return
    notify.notify_user(
        text,
        kind=notify.NotifyKind.INFO,
        idempotency_key=key,
        audience=NotifyAudience.COLLEAGUE_ACTION,
    )
