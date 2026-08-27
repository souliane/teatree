"""Safe ``pass`` writes for Slack token slots — validate-before-write, back-up-before-overwrite.

The ``pass`` store is not version-controlled, so an overwrite is
irreversible and a wrong-slot write (a ``xoxb-`` value in the ``xoxp-``
slot) is silent until the slot-based routing policy mis-sends or drops a
call. The prefix validators in
:mod:`teatree.backends.slack.token_validation` run at *read* time
(backend construction), where a mismatch aborts a loop tick rather than
refusing the bad write up front.

:class:`SlackTokenSlot` pairs each ``pass`` key with the validator its
value must pass. The slot policy is what lives here — the value's prefix
must match the slot before a byte is written, so a bot token cannot reach
the user slot. The back-up-before-overwrite half is
:func:`~teatree.utils.secrets.write_pass_with_backup`, shared with every
other guided token walkthrough.
"""

from collections.abc import Callable
from dataclasses import dataclass

from teatree.backends.slack.token_validation import (
    TokenSlotMismatchError,
    assert_app_token,
    assert_bot_token,
    assert_user_token,
)
from teatree.utils.secrets import SecretStoreError, write_pass_with_backup

type Validator = Callable[[str], None]
type Echo = Callable[[str], None]


class SlackTokenWriteError(RuntimeError):
    """A Slack token write was refused or the backup/insert failed."""


@dataclass(frozen=True, slots=True)
class SlackTokenSlot:
    """A ``pass`` key paired with the prefix validator its value must pass."""

    pass_key: str
    validator: Validator
    slot_name: str


USER_TOKEN_SLOT = SlackTokenSlot("slack/user-oauth-token", assert_user_token, "user (xoxp-)")
BOT_TOKEN_SLOT = SlackTokenSlot("slack/bot-token", assert_bot_token, "bot (xoxb-)")


def bot_token_slot(token_ref: str) -> SlackTokenSlot:
    """The per-overlay bot-token slot (``<token_ref>-bot``)."""
    return SlackTokenSlot(f"{token_ref}-bot", assert_bot_token, "bot (xoxb-)")


def app_token_slot(token_ref: str) -> SlackTokenSlot:
    """The per-overlay app-token slot (``<token_ref>-app``)."""
    return SlackTokenSlot(f"{token_ref}-app", assert_app_token, "app (xapp-)")


def store_slack_token(slot: SlackTokenSlot, value: str, *, echo: Echo) -> str:
    """Validate *value* for *slot*, back up any prior value, then write it.

    Returns the backup key when an existing value was preserved, else
    ``""``. Raises :class:`SlackTokenWriteError` when the value fails its
    prefix validator (no write happens) or when the backup / insert
    itself fails (no clobber happens).
    """
    if not value.strip():
        empty_message = f"refusing to write an empty value to the {slot.slot_name} slot."
        raise SlackTokenWriteError(empty_message)
    try:
        slot.validator(value)
    except TokenSlotMismatchError as exc:
        echo(f"ERROR Refusing to write to the {slot.slot_name} slot ({slot.pass_key}): {exc}")
        raise SlackTokenWriteError(str(exc)) from exc
    try:
        return write_pass_with_backup(slot.pass_key, value, echo=echo)
    except SecretStoreError as exc:
        raise SlackTokenWriteError(str(exc)) from exc


__all__ = [
    "BOT_TOKEN_SLOT",
    "USER_TOKEN_SLOT",
    "SlackTokenSlot",
    "SlackTokenWriteError",
    "app_token_slot",
    "bot_token_slot",
    "store_slack_token",
]
