"""Post a terminal relay into the owner's DM as the bot and prove it landed.

The model cannot reach this module (``core.models`` sits below ``core``), so it is registered with
:mod:`teatree.core.modelkit.gate_registry` at app-ready and fetched by name at call time.
"""

import html

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.modelkit.relay import RelayPosted, RelayRefusal
from teatree.core.models import BotPing
from teatree.core.notify import NotifyKind, notify_user_outcome
from teatree.core.notify_types import NotifyOptions
from teatree.core.slack_first.factory_slack_liveness import SlackLiveVerdict
from teatree.hooks.publish_surface import contains_secret


def post_relay(verdict: SlackLiveVerdict, text: str, *, key: str) -> RelayPosted | RelayRefusal:
    """Post *text* verbatim at the root of the owner DM under *key*, then read it back.

    Idempotent per *key*: a repeat finds the delivered ping and returns the same coordinates.
    """
    if contains_secret(text):
        return RelayRefusal("secret: the prompt carries a credential, so it is not posted to Slack")
    outcome = notify_user_outcome(
        text,
        kind=NotifyKind.RELAY,
        idempotency_key=key,
        audience=NotifyAudience.OWNER_QUESTION,
        options=NotifyOptions(
            backend=verdict.backend, user_id=verdict.owner_id, linkify=False, as_thread_root=True, speak=False
        ),
    )
    if not outcome.sent:
        return RelayRefusal(f"delivery_failed: {outcome.detail}", delivery_failed=True)
    ping = BotPing.objects.filter(idempotency_key=key, status=BotPing.Status.SENT).first()
    if ping is None or not ping.posted_ts:
        return RelayRefusal("delivery_failed: no delivered ping recorded", delivery_failed=True)
    message = verdict.backend.fetch_message(channel=ping.channel_ref, ts=ping.posted_ts) if verdict.backend else {}
    if str(message.get("ts", "")) != ping.posted_ts or html.unescape(text) not in html.unescape(
        str(message.get("text", ""))
    ):
        return RelayRefusal("delivery_failed: the post did not read back from Slack", delivery_failed=True)
    return RelayPosted(channel=ping.channel_ref, ts=ping.posted_ts, permalink=ping.permalink)
