"""What a terminal relay returns: the receipt of a relayed ask, or the refusal that sends it back to the terminal."""

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

#: Slack truncates a message above this many characters.
RELAY_MAX_CHARS = 38_000

#: How long a relayed ask may sit without a confirmed loop reply before it is listed as open.
RELAY_QUIET_AFTER = dt.timedelta(minutes=5)

#: How long a delegated relay's work may stay unclaimed or unfinished before it is listed as open.
RELAY_STALLED_AFTER = dt.timedelta(hours=1)


class LiveVerdict(Protocol):
    """What the model reads off ``factory_slack_live``, which lives above it."""

    @property
    def overlay(self) -> str: ...

    @property
    def live(self) -> bool: ...

    @property
    def reason(self) -> str: ...

    @property
    def owner_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class RelayPosted:
    """The bot's verified post in the owner DM."""

    channel: str
    ts: str
    permalink: str


@dataclass(frozen=True, slots=True)
class RelayReceipt:
    """A relayed ask queued as a ``PendingChatInjection``; ``created`` is ``False`` on a same-id retry."""

    pk: int
    channel: str
    ts: str
    permalink: str
    created: bool = True


@dataclass(frozen=True, slots=True)
class RelayRefusal:
    """The ask was not relayed; ``delivery_failed`` separates a failed or unconfirmed post from a refusal to relay."""

    reason: str
    delivery_failed: bool = False
