"""Value types for the bot→user notification egress.

The typed vocabulary the :mod:`teatree.core.notify` egress and its
:mod:`teatree.core.notify_ledger` audit helpers both speak: the notification
kind, the machine-readable non-delivery reason, the outcome record, and the
optional-override bundle. Split out of ``notify.py`` so the egress and the
ledger share one dependency-free type module with no import cycle.
"""

import enum
from dataclasses import dataclass

from teatree.core.backend_protocols import MessagingBackend
from teatree.types import RawAPIDict


class NotifyKind(enum.StrEnum):
    """Direction of the bot→user notification."""

    ANSWER = "answer"
    QUESTION = "question"
    INFO = "info"


class NotifyReason(enum.StrEnum):
    """Why the egress did (not) deliver — the machine-readable half of :class:`NotifyOutcome`.

    A notification nobody is watching MUST NOT fail as a bare ``False``: the
    caller cannot tell a disabled feature from a dead transport from a
    concurrent tick winning the claim, so nothing downstream can react and
    nothing surfaces to the operator. Every non-delivery branch names itself
    here, and the name is what lands in ``BotPing.error_message``.
    """

    NONE = ""
    INTERNAL_AUDIENCE = "internal_audience"
    FEATURE_DISABLED = "feature_disabled"
    ALREADY_SENT = "already_sent"
    LEDGER_UNAVAILABLE = "ledger_unavailable"
    NO_MESSAGING_BACKEND = "no_messaging_backend"
    AMBIGUOUS_OVERLAY = "ambiguous_overlay"
    NO_USER_ID = "no_user_id"
    CLAIMED_BY_CONCURRENT_TICK = "claimed_by_concurrent_tick"
    DELIVERY_FAILED = "delivery_failed"

    @property
    def detail(self) -> str:
        return _REASON_DETAIL[self]


_REASON_DETAIL: dict[NotifyReason, str] = {
    NotifyReason.NONE: "",
    NotifyReason.INTERNAL_AUDIENCE: "internal audience — logged, never DM'd",
    NotifyReason.FEATURE_DISABLED: "the notify_user feature is disabled in settings",
    NotifyReason.ALREADY_SENT: "already delivered under this idempotency key",
    NotifyReason.LEDGER_UNAVAILABLE: "the BotPing ledger was unreadable (database error)",
    NotifyReason.NO_MESSAGING_BACKEND: (
        "no registered overlay carries a real messaging transport — "
        "configure messaging_backend=slack with bot tokens (t3 setup slack-bot)"
    ),
    NotifyReason.AMBIGUOUS_OVERLAY: (
        "several overlays carry a messaging transport and the active overlay does not name one of them — "
        "export T3_OVERLAY_NAME (or pass --overlay) to the overlay whose workspace the owner reads"
    ),
    NotifyReason.NO_USER_ID: "no owner user_id configured",
    NotifyReason.CLAIMED_BY_CONCURRENT_TICK: "a concurrent tick already claimed delivery",
    NotifyReason.DELIVERY_FAILED: "the messaging backend rejected the delivery",
}


@dataclass(frozen=True, slots=True)
class NotifyOutcome:
    """The result of one egress attempt: delivered or not, and why not."""

    sent: bool
    reason: NotifyReason = NotifyReason.NONE
    error: str = ""

    @property
    def detail(self) -> str:
        return self.error or self.reason.detail


@dataclass(frozen=True, slots=True)
class NotifyOptions:
    """Optional overrides for :func:`teatree.core.notify.notify_user_outcome`, all defaulting to the production path.

    Bundling the opt-in / test-override knobs keeps the egress signature at its
    required core (``text`` + ``kind`` + ``idempotency_key`` + ``audience``); a
    caller injects only the fields it overrides — a test ``backend``, a pinned
    ``user_id``, ``linkify=False`` for pre-linkified text, the ``answering_slack_ts``
    of a question this DM answers, or opaque Block Kit ``blocks``.
    """

    backend: MessagingBackend | None = None
    user_id: str | None = None
    linkify: bool = True
    answering_slack_ts: str = ""
    blocks: list[RawAPIDict] | None = None


#: Delivered outcome — the single shared success record.
DELIVERED = NotifyOutcome(sent=True)

#: The all-default overrides — a frozen singleton so the egress default binds it
#: without a call-in-default (B008) and every unopted caller shares one instance.
DEFAULT_NOTIFY_OPTIONS = NotifyOptions()


def blocked(reason: NotifyReason, *, error: str = "") -> NotifyOutcome:
    """A not-sent :class:`NotifyOutcome` naming its :class:`NotifyReason`."""
    return NotifyOutcome(sent=False, reason=reason, error=error)
