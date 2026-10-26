"""When an unanswered owner question is next re-asked, and the idempotency key of that bump."""

import datetime as dt

from teatree.core.modelkit.fibonacci import fibonacci_bump_index

#: How long one backlog-digest bucket lasts. The digest's idempotency key is the
#: bucket index, so the ``BotPing`` ledger collapses every tick inside a bucket to
#: a single delivered message and a new bucket is the next nag.
RESURFACE_INTERVAL_HOURS = 24

#: The unit of the per-question bump schedule: 1, 2, 4, 7, 12 … hours after the first post.
REASK_UNIT = dt.timedelta(hours=1)

#: The owner is never pinged from 22:00 until 08:00 in their own zone.
OWNER_QUIET_FROM = dt.time(22, 0)
OWNER_QUIET_UNTIL = dt.time(8, 0)

#: Prefix of the ``BotPing`` idempotency key a re-ask writes.
REASK_KEY_PREFIX = "reask:"


def bump_due(first_posted_at: dt.datetime, *, now: dt.datetime) -> int:
    """Which bump a question first posted at *first_posted_at* is due at *now* — ``0`` while none is.

    The gap between bumps WIDENS (1, 1, 2, 3, 5 … hours) instead of repeating, so a
    question nobody can answer yet stops costing the owner a notification an hour forever
    while an uncapped schedule keeps it from being forgotten. Derived from the first post,
    so a bump needs no stored counter and a question answered at any point simply
    leaves the pending set.

    Index ``0`` is the first post itself — bumping it again immediately would double-post
    the same question.
    """
    return fibonacci_bump_index((now - first_posted_at) / REASK_UNIT)


def owner_quiet_at(local_time: dt.time) -> bool:
    """Whether *local_time* (in the owner's zone) lies in the window nothing is sent in."""
    return local_time >= OWNER_QUIET_FROM or local_time < OWNER_QUIET_UNTIL


def first_posted_at(slack_ts: str, created_at: dt.datetime) -> dt.datetime:
    """When a question first reached the owner: its Slack ``ts`` is the post's epoch time, else the row's creation."""
    try:
        return dt.datetime.fromtimestamp(float(slack_ts), tz=dt.UTC)
    except (ValueError, OverflowError, OSError):
        return created_at


def reask_key(notify_ref: str, gap: int) -> str:
    """The bump's idempotency key — the question AND the widening-gap bump it is sending."""
    return f"{REASK_KEY_PREFIX}{notify_ref}:{gap}"
