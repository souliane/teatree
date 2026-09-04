"""Every outbound Slack write, split out of ``bot.py``.

``SlackBotBackend`` keeps the credential/identity facade and the read surface;
the wire calls that PUBLISH — a post, a reaction, and their two
destination-routed variants — live here, so payload assembly and the #1395
voice/token gate sit together instead of being re-derived per method.

The single-emoji refusal deliberately stays at the caller: it must run before
``_channel_token``, which can fire a live Connect-membership probe, so a body
that will be refused never costs an API call.

None of these reaches the Slack API itself. Each takes the backend's ``_post``
as *poster*, so they inherit its bounded-retry transport, its idempotency
classification, and the ``chat.postMessage`` wrap seam — matching how
``open_im_channel`` and the other web helpers are already wired.
"""

import dataclasses
from typing import Protocol

from teatree.types import RawAPIDict

type SlackPayload = dict[str, object]


@dataclasses.dataclass(frozen=True, kw_only=True)
class ChatMessage:
    """What to say and where — the parts of a post that are not credentials."""

    channel: str
    text: str
    thread_ts: str = ""
    blocks: list[RawAPIDict] | None = None

    def payload(self) -> SlackPayload:
        payload: SlackPayload = {"channel": self.channel, "text": self.text}
        if self.thread_ts:
            payload["thread_ts"] = self.thread_ts
        if self.blocks:
            payload["blocks"] = self.blocks  # ``text`` stays the notification + degradation fallback
        return payload


class Poster(Protocol):
    """``SlackBotBackend._post``, structurally typed so this module stays a leaf."""

    def __call__(
        self,
        method: str,
        payload: SlackPayload,
        *,
        token: str = "",
        idempotent: bool = True,
        wrap_exempt_reason: str = "",
    ) -> RawAPIDict: ...  # pragma: no branch


class VoiceGate(Protocol):
    """The #1395 voice/token mismatch check ``publish`` runs before publishing."""

    def check(self, *, text: str, channel: str, token: str) -> None: ...  # pragma: no branch


def publish(
    poster: Poster,
    *,
    token: str,
    voice_gate: VoiceGate,
    message: ChatMessage,
    wrap_exempt_reason: str = "",
) -> RawAPIDict:
    """Post *message* under the Connect-membership-chosen *token*.

    The voice gate reads the author's own text: the #3809 wrap runs later,
    inside *poster*, so the classifier never sees a re-flowed body.
    """
    voice_gate.check(text=message.text, channel=message.channel, token=token)
    return poster(
        "chat.postMessage", message.payload(), token=token, idempotent=False, wrap_exempt_reason=wrap_exempt_reason
    )


def add_reaction(poster: Poster, *, token: str, channel: str, ts: str, emoji: str) -> RawAPIDict:
    """Add *emoji* to *channel*'s message under the Connect-membership-chosen *token*."""
    return poster("reactions.add", {"channel": channel, "timestamp": ts, "name": emoji}, token=token)


def publish_routed(poster: Poster, *, token: str, message: ChatMessage, wrap_exempt_reason: str = "") -> RawAPIDict:
    """Post *message* under the destination-chosen *token* (#1750).

    The deterministic edge for ``t3 <overlay> notify post``. Returns the raw
    Slack body so the CLI can inspect ``ok`` / ``error``; ``{}`` when no token at
    all is configured, so no post is attempted without a credential.
    """
    if not token:
        return {}
    return poster(
        "chat.postMessage", message.payload(), token=token, idempotent=False, wrap_exempt_reason=wrap_exempt_reason
    )


def add_reaction_routed(poster: Poster, *, token: str, channel: str, ts: str, emoji: str) -> RawAPIDict:
    """Add a reaction to *channel*'s message under the destination-chosen *token* (#1750).

    Reacting follows the *same* routing rule as :func:`publish_routed`. Returns the
    raw Slack body; ``{}`` when no token is configured.
    """
    if not token:
        return {}
    return add_reaction(poster, token=token, channel=channel, ts=ts, emoji=emoji)
