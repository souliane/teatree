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

:func:`retire_head_claim` is the write half of the same rule, shared by the same
tables for the same reason: "when is a per-head claim spent?" also has one answer.
"""

import datetime as dt
from collections.abc import Iterable
from typing import TypeVar

from django.db import models
from django.utils import timezone

_ClaimT = TypeVar("_ClaimT", bound=models.Model)


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


def retire_head_claim(
    in_flight: "models.QuerySet[_ClaimT]",
    *,
    slug: str,
    pr_id: int,
    head_sha: str,
    to_state: str,
) -> bool:
    """Compare-and-set ONE in-flight per-head claim into a terminal state. ``True`` iff a row moved.

    The write half of what :func:`acquirable_q` is the read half of, and shared for the
    same reason. TWO tables claim the review path per ``(slug, pr_id, head_sha)`` — the
    #68 dispatch ledger and the codex / self-PR marker — and both retire on the same two
    events: a recorded verdict, and a verdict the recorder is structurally unable to
    record. A second hand-written copy of this ``UPDATE`` is how the two ledgers would
    come to disagree about what "spent" means, and about which key identifies a head,
    which is the drift the one shared predicate exists to prevent.

    *in_flight* is the caller's claims already restricted to its ACTIVE states, so a claim
    that is already terminal is never re-transitioned: a RESOLVED head carries a verdict
    and a REFUSED one carries a page, and overwriting either would replace a durable fact
    with a weaker one. Retiring an unclaimed head is a legitimate no-op — most verdicts
    conclude a review neither table armed — so this returns ``False`` rather than raising.

    The key is normalised the way the claim tables store it (stripped slug, stripped and
    lowercased head), because what reaches the callers is a reviewer's self-reported SHA.
    """
    return bool(
        in_flight.filter(slug=slug.strip(), pr_id=pr_id, head_sha=head_sha.strip().lower()).update(
            state=to_state, resolved_at=timezone.now()
        )
    )
