"""The one spelling of an expiry-acquirable claim (#3920).

Several tables on the review/merge path are the same thing: an idempotent claim
recorded before a side effect, keyed on (target, head), held by a worker that may
die. :class:`~teatree.core.models.mr_review_lock.MRReviewLock` got this right —
a compare-and-set over a state field with a ``deadline``, so a crashed holder's
claim becomes acquirable again instead of blocking forever. The dispatch ledgers
(:class:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch`,
:class:`~teatree.core.models.critic_dispatch.CriticDispatch`) did not: they were
``get_or_create`` with no terminal state, no deadline and no reaper, so a
reviewing task that died left the head permanently un-armable.

This module is the predicate all three share, so "when may a claim be taken?" has
one answer rather than three. A claim is acquirable when it is in a state that is
always acquirable, or when it is still ACTIVE and its deadline has passed. Any
other state is terminal: the work concluded and the claim is spent.

Both membership sets are explicit because the two tables disagree about what
``resolved`` means, and the disagreement is real rather than accidental. A
resolved lock is per-MR and freely re-acquirable — a later push dispatches a
fresh review on the same MR. A resolved dispatch is per-HEAD and terminal — a
verdict already covers that exact tree, so re-arming it would be review churn.
"""

import datetime as dt
from collections.abc import Iterable

from django.db import models


def acquirable_q(
    *,
    always_acquirable: Iterable[str],
    active: Iterable[str],
    now: dt.datetime,
    state_field: str = "state",
    deadline_field: str = "deadline",
) -> models.Q:
    """The claim-acquirability predicate, as a ``Q`` for a CAS ``.filter(...).update(...)``.

    *always_acquirable* are the states a claim may be taken from regardless of
    its deadline (an idle or released claim). *active* are the in-flight states,
    acquirable only once *now* is past the row's deadline — a holder that never
    came back. Any state in neither set is terminal and never acquirable.

    A NULL deadline on an active row reads as "no bound", so it is not stolen by
    expiry: an unbounded claim is released by its holder or not at all.
    """
    expired_active = models.Q(**{f"{state_field}__in": list(active), f"{deadline_field}__lt": now})
    return models.Q(**{f"{state_field}__in": list(always_acquirable)}) | expired_active
