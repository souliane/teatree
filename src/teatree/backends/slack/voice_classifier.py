"""Voice/token mismatch gate for outbound Slack posts (#1395).

A pre-publish classifier between ``chat.postMessage`` and the Slack API
that refuses (in strict mode) or warns (in warn mode, the
backward-compat default) when the message body's *voice* and the
*token kind* it would go out under disagree.

The recurrence this guards against. Sub-agents repeatedly produced
agent-voice status DMs ("PR merged", "task completed", "evidence at …")
and pushed them through the user's personal ``xoxp-`` token to the
user's own DM channel. Slack rendered those as user-to-self messages
and does NOT raise a phone notification on a self-DM, so the user
missed multiple coordination signals in a single session. The prose
rule (``feedback_agent_to_user_dms_must_use_bot_token``) failed across
six sub-agents the same day it was saved; this gate is the
deterministic structural fix.

The mirror failure mode this also guards against. A user-voice review
request ("please review !6264", "I'd appreciate a look") posted under
the bot token is the inverse mismatch — the message reads to
colleagues as a bot announcement rather than the operator's personal
request, breaking the per-overlay routing sibling rule
``feedback_review_request_post_via_personal_token_only``.

The classifier is intentionally pure: text in, verdict out. The
:class:`SlackBotBackend` wires it into ``post_message`` / ``post_reply``
so every backend-mediated Slack post is gated; sub-agents that curl
the Slack API directly are out of scope of this surface (they bypass
the backend entirely — the issue describes a sibling stopgap of
patching the dispatch prompt itself, not a runtime hook).
"""

import logging
from enum import StrEnum

from teatree.types import SlackVoiceClassifierMode as ClassifierMode

__all__ = [
    "ClassifierMode",
    "SlackVoiceMismatchError",
    "TokenKind",
    "Voice",
    "VoiceTokenGate",
    "assert_voice_token_match",
    "classify_token",
    "classify_voice",
]


_log = logging.getLogger(__name__)


class Voice(StrEnum):
    """The detected speaker-identity of a Slack message body."""

    USER = "user"
    AGENT = "agent"
    AMBIGUOUS = "ambiguous"


class TokenKind(StrEnum):
    """The detected identity of a Slack OAuth token by prefix."""

    USER = "user"
    BOT = "bot"
    UNKNOWN = "unknown"


class SlackVoiceMismatchError(ValueError):
    """A Slack post's body voice does not match its token kind (#1395).

    Inherits from ``ValueError`` so factory callers that already catch
    ``ValueError`` around backend construction demote it to a skipped
    post rather than a process crash.
    """


_AGENT_VOICE_MARKERS: tuple[str, ...] = (
    "the agent",
    "i've shipped",
    "pr merged",
    "mr merged",
    "mr ready",
    "pr ready",
    "evidence",
    "draft note",
    "dm permalink",
    "mr url",
    "pipeline",
    "task completed",
    "task complete",
    "on-behalf",
    "on behalf of the agent",
    "co-authored-by",
)

_USER_VOICE_MARKERS: tuple[str, ...] = (
    "please review",
    "i'd appreciate",
    "rr for",
    "on behalf of",
)


def classify_voice(text: str) -> Voice:
    """Detect the speaker-identity of *text*.

    Returns :attr:`Voice.AGENT` when an agent-status marker fires and no
    user-voice marker does, :attr:`Voice.USER` when a user-voice marker
    fires and no agent marker does, and :attr:`Voice.AMBIGUOUS` when
    neither set fires or both fire (mixed voice — an agent quoting a
    user request, a user paraphrasing an agent status). The mismatch
    gate intentionally never refuses on :attr:`Voice.AMBIGUOUS` —
    confident verdicts only.
    """
    lower = text.lower()
    has_agent = any(marker in lower for marker in _AGENT_VOICE_MARKERS)
    has_user = any(marker in lower for marker in _USER_VOICE_MARKERS)
    if has_agent and not has_user:
        return Voice.AGENT
    if has_user and not has_agent:
        return Voice.USER
    return Voice.AMBIGUOUS


def classify_token(token: str) -> TokenKind:
    """Detect the kind of a Slack OAuth token from its prefix.

    Unknown / empty / non-Slack-shaped tokens map to
    :attr:`TokenKind.UNKNOWN` so the gate degrades open on a noop
    backend, a unit-test stub, or a future token shape we have not yet
    taught the classifier about.
    """
    if token.startswith("xoxp-"):
        return TokenKind.USER
    if token.startswith("xoxb-"):
        return TokenKind.BOT
    return TokenKind.UNKNOWN


def _agent_voice_user_token_to_user_dm(
    *,
    voice: Voice,
    token_kind: TokenKind,
    channel: str,
    dm_channel_ids: set[str],
) -> bool:
    """The #1395 recurrence: agent-voice over xoxp to the user's own DM."""
    return voice is Voice.AGENT and token_kind is TokenKind.USER and channel in dm_channel_ids


def _user_voice_bot_token(*, voice: Voice, token_kind: TokenKind) -> bool:
    """The mirror failure: user-voice review-request via bot token."""
    return voice is Voice.USER and token_kind is TokenKind.BOT


def _build_mismatch_message(
    *,
    voice: Voice,
    token_kind: TokenKind,
    channel: str,
    text: str,
) -> str:
    snippet = text.strip().replace("\n", " ")[:80]
    if voice is Voice.AGENT:
        corrective = (
            "Agent-voice status messages must go out under the bot token "
            "(`xoxb-…` via `messaging_from_overlay(overlay_name=...)`); "
            "the personal user token (`xoxp-…`) to the user's own DM "
            "channel renders as user-to-self and Slack does NOT notify "
            "on self-DMs."
        )
    else:
        corrective = (
            "User-voice messages (review requests, personal asks) must "
            "go out under the personal user token (`xoxp-…`); the bot "
            "token misattributes the message and breaks per-overlay "
            "voice routing."
        )
    return (
        f"Slack voice/token mismatch refused: voice={voice.value!r} "
        f"token={token_kind.value!r} channel={channel!r} "
        f"body={snippet!r}. {corrective} (souliane/teatree#1395)"
    )


class VoiceTokenGate:
    """Per-backend voice/token mismatch gate (#1395).

    Holds the configured strictness mode and the set of channel ids
    the gate treats as the user's own DM. The :class:`SlackBotBackend`
    composes one and routes every outbound ``chat.postMessage`` body
    through :meth:`check` immediately before the Slack API call.
    """

    __slots__ = ("dm_channel_id", "mode")

    def __init__(self, *, mode: ClassifierMode = ClassifierMode.WARN, dm_channel_id: str = "") -> None:
        self.mode = mode
        self.dm_channel_id = dm_channel_id

    def check(self, *, text: str, channel: str, token: str) -> None:
        assert_voice_token_match(
            text=text,
            channel=channel,
            token=token,
            mode=self.mode,
            dm_channel_ids={self.dm_channel_id} if self.dm_channel_id else set(),
        )


def assert_voice_token_match(
    *,
    text: str,
    channel: str,
    token: str,
    mode: ClassifierMode,
    dm_channel_ids: set[str],
) -> None:
    """Raise :class:`SlackVoiceMismatchError` on a confident mismatch.

    The gate fires on two confident mismatches:

    *   :attr:`Voice.AGENT` + :attr:`TokenKind.USER` + *channel in
        ``dm_channel_ids``* — the #1395 recurrence (agent status DM
        going out under the user's personal token, rendering as
        user-to-self with no notification).
    *   :attr:`Voice.USER` + :attr:`TokenKind.BOT` — the mirror failure
        (user-voice review request misattributed to the bot).

    :attr:`Voice.AMBIGUOUS` and :attr:`TokenKind.UNKNOWN` are never
    refused; the gate trades coverage for false-positive safety so
    legitimate posts cannot be silently dropped.

    In :attr:`ClassifierMode.WARN` (default for backward-compat) the
    helper logs the mismatch at WARNING and returns; in
    :attr:`ClassifierMode.OFF` it returns silently.
    """
    if mode is ClassifierMode.OFF:
        return

    voice = classify_voice(text)
    token_kind = classify_token(token)

    mismatch = _agent_voice_user_token_to_user_dm(
        voice=voice,
        token_kind=token_kind,
        channel=channel,
        dm_channel_ids=dm_channel_ids,
    ) or _user_voice_bot_token(voice=voice, token_kind=token_kind)
    if not mismatch:
        return

    message = _build_mismatch_message(
        voice=voice,
        token_kind=token_kind,
        channel=channel,
        text=text,
    )
    if mode is ClassifierMode.STRICT:
        raise SlackVoiceMismatchError(message)
    _log.warning(message)
