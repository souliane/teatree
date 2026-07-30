"""Notification-relevance audience taxonomy — the deny-by-default owner-DM policy.

Every :func:`teatree.core.notify.notify_user` call declares WHO the DM is for.
The owner reads only four classes of bot→user DM: a real question needing his
decision (:attr:`~NotifyAudience.OWNER_QUESTION`), a shipped/merged delivery
(:attr:`~NotifyAudience.OWNER_DELIVERY`), an outage/HALT escalation
(:attr:`~NotifyAudience.OWNER_ESCALATION`), and an outward-facing act on a
colleague's work (:attr:`~NotifyAudience.COLLEAGUE_ACTION`). Everything else —
flag signals, substrate holds, waiting digests, provision/scope/preset internals
— is :attr:`~NotifyAudience.INTERNAL`: it is logged and terminally recorded, but
never DM'd.

The taxonomy is deliberately closed and coupling-light (only :mod:`enum`) so the
``BotPing`` model can import it to filter its re-delivery backlog without a cycle.
"""

import enum


class NotifyAudience(enum.StrEnum):
    """Who a bot→user notification is for — the routing decision for ``notify_user``."""

    OWNER_QUESTION = "owner_question"
    OWNER_DELIVERY = "owner_delivery"
    OWNER_ESCALATION = "owner_escalation"
    COLLEAGUE_ACTION = "colleague_action"
    INTERNAL = "internal"


#: The audiences the owner actually reads — the only ones a DM is sent for and the
#: only ones the cross-tick drain re-delivers. ``INTERNAL`` is excluded by design.
OWNER_AUDIENCES: frozenset[NotifyAudience] = frozenset(
    {
        NotifyAudience.OWNER_QUESTION,
        NotifyAudience.OWNER_DELIVERY,
        NotifyAudience.OWNER_ESCALATION,
        NotifyAudience.COLLEAGUE_ACTION,
    }
)

#: The ``.value`` strings of :data:`OWNER_AUDIENCES` — the form persisted on the
#: ``BotPing.audience`` column, so the model can filter without importing the enum
#: type into a QuerySet ``__in`` lookup.
OWNER_AUDIENCE_VALUES: frozenset[str] = frozenset(a.value for a in OWNER_AUDIENCES)


__all__ = ["OWNER_AUDIENCES", "OWNER_AUDIENCE_VALUES", "NotifyAudience"]
