"""When an unanswered owner question is next re-asked, and the idempotency key of that bump."""

import datetime as dt

from teatree.core.modelkit.fibonacci import fibonacci_bump_index

#: How long one backlog-digest bucket lasts. The digest's idempotency key is the
#: bucket index, so the ``BotPing`` ledger collapses every tick inside a bucket to
#: a single delivered message and a new bucket is the next nag.
RESURFACE_INTERVAL_HOURS = 24

#: Prefix of the ``BotPing`` idempotency key a re-ask writes.
REASK_KEY_PREFIX = "reask:"


def bump_due(created_at: dt.datetime, *, now: dt.datetime) -> int:
    """Which bump a question created at *created_at* is due at *now* — ``0`` while none is.

    The gap between bumps WIDENS (1, 1, 2, 3, 5 … intervals) instead of repeating, so a
    question nobody can answer yet stops costing the owner a notification a day forever
    while an uncapped schedule keeps it from being forgotten. Derived from the row's own
    age, so a bump needs no stored counter and a question answered at any point simply
    leaves the pending set.

    Index ``0`` is the mirror drain's first post, which has just happened for a freshly
    mirrored row — bumping it again immediately would double-post the same question.
    """
    elapsed = (now - created_at).total_seconds() / (RESURFACE_INTERVAL_HOURS * 3600)
    return fibonacci_bump_index(elapsed)


def reask_key(notify_ref: str, gap: int) -> str:
    """The bump's idempotency key — the question AND the widening-gap bump it is sending."""
    return f"{REASK_KEY_PREFIX}{notify_ref}:{gap}"
