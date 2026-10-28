r"""Errors that enforce ``t3 slack react`` as the only reaction surface (#1281).

Two structural invariants live here:

1.  :class:`SlackReactionError` raises on Slack ``reactions.add`` refusal,
    preventing a silent fallback to a bare-emoji thread reply.
2.  :class:`SingleEmojiBodyRefusedError` rejects bare-emoji message bodies
    and directs callers to ``t3 slack react``.

The module has no Slack-API imports, so callers can use it without an import cycle.
"""

import re

__all__ = [
    "SLACK_REACTION_REMEDIATION",
    "SingleEmojiBodyRefusedError",
    "SlackReactionError",
    "build_react_error_message",
    "is_single_emoji_body",
]


_SINGLE_EMOJI_BODY = re.compile(r"^:[a-z0-9_+\-]+:$")


def is_single_emoji_body(text: str) -> bool:
    """Return ``True`` when *text* stripped is a bare ``:emoji:`` token.

    Empty strings, normal prose, and prose that *contains* an emoji
    (``"Approved :white_check_mark:."``) all return ``False`` — only the
    standalone-emoji shape is banned, since prose with an emoji inside it
    is a legitimate reply that doesn't masquerade as a reaction.
    """
    stripped = text.strip()
    if not stripped:
        return False
    return bool(_SINGLE_EMOJI_BODY.match(stripped))


SLACK_REACTION_REMEDIATION = (
    "Fix by re-running `t3 setup slack-user-token` (provisions the personal "
    "xoxp token with reactions:write; see souliane/teatree#1232). "
    "Do NOT fall back to posting a `:emoji:` thread reply — that is thread "
    "spam under the user's name, not a reaction."
)


def build_react_error_message(error_code: str, channel: str, ts: str) -> str:
    """Compose the operator-facing message for a :class:`SlackReactionError`."""
    return f"Slack reactions.add refused on {channel}/{ts} with error={error_code!r}. {SLACK_REACTION_REMEDIATION}"


class SlackReactionError(RuntimeError):
    """Slack's ``reactions.add`` returned ``ok:false`` (#1281).

    Carries the raw Slack error code (``missing_scope``, ``not_in_channel``,
    ``mcp_externally_shared_channel_restricted``, etc.) so callers can
    branch on it without re-parsing the message. The message itself
    points the operator at the documented remediation, which forbids
    the thread-emoji fallback.

    ``already_reacted`` is **not** raised — it is the idempotent
    success case (the desired end state is the reaction being present).
    Callers should treat it as success and never construct this class
    with that code.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class SingleEmojiBodyRefusedError(ValueError):
    """A ``chat.postMessage`` body of ``:emoji:`` shape is refused (#1281).

    The single-emoji body is the substitute the agent produced when
    ``reactions.add`` failed (missing_scope, restricted channel). Banning
    the shape at the backend boundary forecloses the silent fallback
    path. The CLI surface for an actual reaction is ``t3 slack react``;
    the error message says so explicitly so any operator (or future
    agent) hitting this gate is steered to the right tool.
    """

    def __init__(self, body: str) -> None:
        super().__init__(
            f"Refusing to post single-emoji body {body!r} via chat.postMessage. "
            "A single :emoji: thread reply is thread spam, not a reaction — "
            "use `t3 slack react <channel> <ts> <emoji>` instead (#1281)."
        )
        self.body = body
