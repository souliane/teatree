"""Prefix-validation for Slack tokens at backend construction (#1285).

Codex adversarial review #1282 item 5 surfaced this gap: PR #1279 added a
capture-time regex in ``slack_user_token_setup._prompt_user_token``, but
once a token sits in ``pass`` as a raw string the runtime construction
path (``backend_factory._messaging_from_toml``,
``backends.loader.get_messaging``) reads it back and threads it into a
``SlackBotBackend`` slot with no prefix check. The deterministic policy
in :mod:`teatree.backends.slack.token_policy` then routes by *slot*,
never by prefix, so a swapped token (the field bug observed
2026-05-20: an ``xoxb-…`` ended up at the ``xoxp-`` user slot) either
impersonates the user or — worse — is silently dropped on a Slack-Connect
externally-shared channel that rejects the wrong token.

For the *bot* and *app* slots the gate stays strict everywhere: a wrong
token there is a genuine misconfiguration that must surface loudly, and
silently dropping it would hide the broken bot. The gate fires inside
``SlackBotBackend.__init__`` (the single chokepoint every factory path
hits) so the loop fails *loudly but early*: a ``TokenSlotMismatchError``
with the offending slot named and the right ``t3 setup …`` command
pointed at, never a mid-tick crash. Empty tokens stay valid — they are
the legitimate no-credential / bot-only / no-socket-mode cases the rest
of the code already tolerates.

The *user* slot is different: it is optional capability (colleague-channel
posts/reactions under the human identity on Slack-Connect channels). A
malformed user token must NOT wedge the autonomous loop — merges, CI, PR
sweeps, ticket scans — on a Slack-only credential typo. The loop
construction paths (:func:`teatree.backends.loader.get_messaging`,
``backend_factory._messaging_from_toml``) therefore call
:func:`resolve_user_token_or_degrade`: a prefix-mismatched user token
degrades to bot-only (treated as absent, with a one-time WARNING naming
``t3 setup slack-user-token``) instead of raising. The explicit
setup/provision path keeps :func:`assert_user_token` so a wrong paste
there still errors loudly — strictness is preserved where a human is at
the keyboard fixing the credential.
"""

import logging
import re

logger = logging.getLogger(__name__)

BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]+$")
USER_TOKEN_RE = re.compile(r"^xoxp-[A-Za-z0-9-]+$")
APP_TOKEN_RE = re.compile(r"^xapp-[A-Za-z0-9-]+$")

USER_SLOT_DEGRADE_WARNING = (
    "Slack user credential is malformed (must start with 'xoxp-') — "
    "degrading to bot-only: colleague-channel posts and reactions under "
    "your identity are disabled this run. Bot DMs and all non-Slack loop "
    "work continue. Fix with `t3 setup slack-user-token`."
)


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


def resolve_user_token_or_degrade(token: str) -> str:
    """Return *token* if it is empty or a valid ``xoxp-`` user token, else ``""``.

    A prefix-mismatched user token degrades to bot-only with a one-time
    WARNING rather than raising — the user token is optional capability,
    so a bad one must never wedge the loop. Use on the loop construction
    paths only; the explicit setup/provision path keeps
    :func:`assert_user_token` so a wrong paste there errors loudly.
    """
    if not token or USER_TOKEN_RE.match(token):
        return token
    logger.warning(USER_SLOT_DEGRADE_WARNING)
    return ""


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
    "USER_SLOT_DEGRADE_WARNING",
    "USER_TOKEN_RE",
    "TokenSlotMismatchError",
    "assert_app_token",
    "assert_bot_token",
    "assert_user_token",
    "resolve_user_token_or_degrade",
]
