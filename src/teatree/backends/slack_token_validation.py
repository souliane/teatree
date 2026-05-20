"""Prefix-validation for Slack tokens at backend construction (#1285).

Codex adversarial review #1282 item 5 surfaced this gap: PR #1279 added a
capture-time regex in ``slack_user_token_setup._prompt_user_token``, but
once a token sits in ``pass`` as a raw string the runtime construction
path (``backend_factory._messaging_from_toml``,
``backends.loader.get_messaging``) reads it back and threads it into a
``SlackBotBackend`` slot with no prefix check. The deterministic policy
in :mod:`teatree.backends.slack_token_policy` then routes by *slot*,
never by prefix, so a swapped token (the field bug observed
2026-05-20: an ``xoxb-…`` ended up at the ``xoxp-`` user slot) either
impersonates the user or — worse — is silently dropped on a Slack-Connect
externally-shared channel that rejects the wrong token.

The gate fires inside ``SlackBotBackend.__init__`` (the single chokepoint
every factory path hits) so the loop fails *loudly but early*: a
``TokenSlotMismatchError`` with the offending slot named and the right
``t3 setup …`` command pointed at, never a mid-tick crash. Empty tokens
stay valid — they are the legitimate no-credential / bot-only /
no-socket-mode cases the rest of the code already tolerates.
"""

import re

BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]+$")
USER_TOKEN_RE = re.compile(r"^xoxp-[A-Za-z0-9-]+$")
APP_TOKEN_RE = re.compile(r"^xapp-[A-Za-z0-9-]+$")


class TokenSlotMismatchError(ValueError):
    """A Slack token's prefix does not match its construction slot.

    Inherits from ``ValueError`` so factory callers that already catch
    ``ValueError`` around backend construction (see
    ``iter_overlay_backends``) demote it to a "skipped overlay" the same
    way they handle credential errors today — never a silent runtime
    drop, but also never a process crash.
    """


def assert_bot_token(token: str) -> None:
    """Reject ``token`` if it is non-empty and lacks the ``xoxb-`` prefix."""
    if not token:
        return
    if not BOT_TOKEN_RE.match(token):
        message = (
            "bot_token must start with 'xoxb-' (Slack bot OAuth token). "
            "Got a token with a different prefix in the bot slot — "
            "this is the runtime half of codex #1282 item 5. "
            "Re-run `t3 setup slack-bot` to capture a correctly-prefixed token."
        )
        raise TokenSlotMismatchError(message)


def assert_user_token(token: str) -> None:
    """Reject ``token`` if it is non-empty and lacks the ``xoxp-`` prefix."""
    if not token:
        return
    if not USER_TOKEN_RE.match(token):
        message = (
            "user_token must start with 'xoxp-' (Slack user OAuth token). "
            "Got a token with a different prefix in the user slot — "
            "this is the exact failure mode observed on 2026-05-20 "
            "(an xoxb- token pasted into the xoxp slot). "
            "Re-run `t3 setup slack-user-token` to capture a correctly-prefixed token."
        )
        raise TokenSlotMismatchError(message)


def assert_app_token(token: str) -> None:
    """Reject ``token`` if it is non-empty and lacks the ``xapp-`` prefix."""
    if not token:
        return
    if not APP_TOKEN_RE.match(token):
        message = (
            "app_token must start with 'xapp-' (Slack Socket Mode app-level token). "
            "Got a token with a different prefix in the app slot. "
            "Re-run `t3 setup slack-bot` to capture a correctly-prefixed app token."
        )
        raise TokenSlotMismatchError(message)


__all__ = [
    "APP_TOKEN_RE",
    "BOT_TOKEN_RE",
    "USER_TOKEN_RE",
    "TokenSlotMismatchError",
    "assert_app_token",
    "assert_bot_token",
    "assert_user_token",
]
