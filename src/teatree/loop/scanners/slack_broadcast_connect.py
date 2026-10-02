"""The Slack-Connect restriction seam of the broadcast scanner (#1131).

A Connect channel rejects the bot token; when the routed token fails too, the
scanner must fail loudly rather than drop the reaction. Carved out of
:mod:`teatree.loop.scanners.slack_broadcasts` by concern (module-health cap).
"""


class ConnectChannelBotRestrictedError(RuntimeError):
    """Raised when a broadcast in a Slack-Connect channel cannot be reacted to.

    A Connect channel rejects the bot token, so
    :func:`teatree.backends.slack.token_policy.channel_token` already routes every
    WRITE there to the personal ``xoxp``. Reaching this error therefore means that
    routed token failed too — no user token is configured (the policy falls back to
    the rejected bot token), or the user token lacks ``reactions:write`` or
    membership of the partner channel. The scanner hard-fails rather than silently
    swallowing the dropped reaction; the error carries the channel id so callers can
    surface a single actionable message.
    """

    def __init__(self, channel: str) -> None:
        super().__init__(
            f"Slack-Connect channel {channel!r} rejected the reaction under the routed "
            "token. Provision the personal token with `t3 setup slack-user-token` and "
            "confirm it carries reactions:write and membership of the channel. "
            "Scanner is failing loudly per #1131 to avoid silent drops.",
        )
        self.channel = channel


def looks_like_connect_restriction(exc: BaseException) -> bool:
    """Heuristic for the Slack-Connect restricted-reaction error shape.

    Slack returns ``not_in_channel`` / ``channel_not_found`` / ``is_ext_shared``
    for the Connect-restricted case. ``SlackBotBackend.react`` posts
    ``reactions.add`` through the transport directly rather than through
    :mod:`teatree.backends.slack.reactions`, so the typed
    :class:`~teatree.backends.slack.react_errors.SlackReactionError` never reaches
    this scanner — the error code arrives only inside a generic exception's
    message, which is what the match below reads.
    """
    message = str(exc)
    return any(token in message for token in ("not_in_channel", "channel_not_found", "is_ext_shared"))


__all__ = ["ConnectChannelBotRestrictedError", "looks_like_connect_restriction"]
