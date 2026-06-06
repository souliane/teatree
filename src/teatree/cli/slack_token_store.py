"""Safe ``pass`` writes for Slack token slots — validate-before-write, back-up-before-overwrite.

The ``pass`` store is not version-controlled, so an overwrite is
irreversible and a wrong-slot write (a ``xoxb-`` value in the ``xoxp-``
slot) is silent until the slot-based routing policy mis-sends or drops a
call. The prefix validators in
:mod:`teatree.backends.slack.token_validation` run at *read* time
(backend construction), where a mismatch aborts a loop tick rather than
refusing the bad write up front.

:class:`SlackTokenSlot` pairs each ``pass`` key with the validator its
value must pass, and :func:`store_slack_token` enforces two invariants
before any ``pass insert``:

1.  **Validate before write.** A value whose prefix does not match the
    slot is refused — no write happens — so a bot token cannot reach the
    user slot.
2.  **Back up before overwrite.** An existing value is copied to a
    timestamped ``<key>.bak-<UTC stamp>`` sibling key before the
    overwrite, keeping the prior token recoverable on a non-git store.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from teatree.backends.slack.token_validation import (
    TokenSlotMismatchError,
    assert_app_token,
    assert_bot_token,
    assert_user_token,
)
from teatree.utils.secrets import read_pass, write_pass

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


def _backup_key(pass_key: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{pass_key}.bak-{stamp}"


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

    backup_key = _back_up_existing(slot, echo=echo)

    if not write_pass(slot.pass_key, value):
        insert_message = f"`pass insert {slot.pass_key}` failed — token not stored."
        raise SlackTokenWriteError(insert_message)
    return backup_key


def _back_up_existing(slot: SlackTokenSlot, *, echo: Echo) -> str:
    existing = read_pass(slot.pass_key)
    if not existing:
        return ""
    backup_key = _backup_key(slot.pass_key)
    if not write_pass(backup_key, existing):
        backup_message = (
            f"could not back up the existing {slot.slot_name} token to `{backup_key}` — refusing to overwrite."
        )
        raise SlackTokenWriteError(backup_message)
    echo(f"OK    Backed up the existing {slot.slot_name} token to `pass {backup_key}` before overwriting.")
    return backup_key


__all__ = [
    "BOT_TOKEN_SLOT",
    "USER_TOKEN_SLOT",
    "SlackTokenSlot",
    "SlackTokenWriteError",
    "app_token_slot",
    "bot_token_slot",
    "store_slack_token",
]
