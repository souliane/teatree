"""Atomic per-MR review-dispatch dedup + merge hold (#1405).

Two independent review-dispatch paths exist: a human/orchestrator manually
spawning a ``t3:reviewer`` sub-agent via the ``Agent()`` tool, and the loop's
:class:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch` scanner
enqueue. Neither path knew about the other, so a manually-dispatched review
already in flight for an MR did not stop the loop from enqueuing a second,
duplicate reviewer for the SAME MR on the very next tick (the observed
recurrence: five manual dispatches in flight, the next loop tick enqueued
five duplicates for the same five MRs).

``MRReviewLock`` is the single per-MR lock both paths acquire before
dispatching. It carries an explicit state machine:

    idle -> review_dispatched -> verdict_pending -> resolved

A lock is ``idle`` when no row exists yet (or after ``reconcile_stale``
clears a dead dispatch) and after ``resolve`` clears an old cycle back to a
fresh acquirable state (``resolved`` is itself acquirable — a later push can
dispatch a fresh review on the same MR). ``review_dispatched`` and
``verdict_pending`` are the two "in flight" states: a lock in either state is
held, and both a competing dispatch attempt and a merge attempt are refused
while it holds. ``resolve`` (called when a :class:`ReviewVerdict
<teatree.core.models.review_verdict.ReviewVerdict>` is recorded — merge_safe
or hold, either way the review concluded) transitions back to ``resolved``.
A ``deadline`` set at acquire time bounds how long a dispatch may hold the
lock: a crashed reviewer's lock is treated as unlocked once its deadline
passes (both by a fresh ``acquire`` and by the merge-gate consult), and
``reconcile_stale`` is the explicit sweep that resets an expired row back to
``idle`` so a stale row is never left masquerading as held.

Keyed on ``(slug, pr_id)`` rather than the MR/PR web URL string itself — the
same repo-identity key :class:`~teatree.core.models.review_verdict.ReviewVerdict`
and :class:`~teatree.core.models.merge_clear.MergeClear` use — because that is
what the merge decision point (:func:`teatree.core.merge.execution.execute_bound_merge`)
already has in hand; ``mr_url`` is carried alongside for display and for the
URL-taking callers (:meth:`acquire_by_url`).
"""

import datetime as dt
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.utils.url_slug import pr_ref_from_url

DEFAULT_LOCK_TTL = dt.timedelta(hours=2)


class MRReviewLock(models.Model):
    """One per-MR review-dispatch lock, keyed on ``(slug, pr_id)``."""

    class State(models.TextChoices):
        IDLE = "idle", "Idle"
        REVIEW_DISPATCHED = "review_dispatched", "Review dispatched"
        VERDICT_PENDING = "verdict_pending", "Verdict pending"
        RESOLVED = "resolved", "Resolved"

    _ACTIVE_STATES: ClassVar[frozenset[str]] = frozenset({State.REVIEW_DISPATCHED, State.VERDICT_PENDING})
    _ACQUIRABLE_STATES: ClassVar[frozenset[str]] = frozenset({State.IDLE, State.RESOLVED})

    slug = models.CharField(max_length=255)
    pr_id = models.IntegerField()
    mr_url = models.URLField(max_length=512, blank=True, default="")
    state = models.CharField(max_length=32, choices=State.choices, default=State.IDLE)
    holder = models.CharField(max_length=255, blank=True, default="")
    dispatched_at = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_mr_review_lock"
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["slug", "pr_id"], name="uniq_mrreviewlock_slug_pr"),
        ]

    def __str__(self) -> str:
        return f"mr-review-lock<{self.pk}:{self.slug}#{self.pr_id} {self.state} holder={self.holder!r}>"

    @classmethod
    def acquire(
        cls,
        *,
        slug: str,
        pr_id: int,
        holder: str,
        mr_url: str = "",
        ttl: dt.timedelta = DEFAULT_LOCK_TTL,
    ) -> "MRReviewLock | None":
        """Atomically claim the lock for ``(slug, pr_id)`` — get-or-create + CAS on state.

        Returns the claimed row on success. Returns ``None`` when a non-stale
        row is already held (``review_dispatched`` / ``verdict_pending`` with
        a deadline still in the future) — the caller's no-op-with-a-pointer-
        to-the-holder case; read ``MRReviewLock.objects.get(slug=..., pr_id=...)``
        for the holder identity. A row in ``idle``/``resolved``, or a stale
        held row (``deadline`` in the past), is acquirable.
        """
        normalized_slug = slug.strip()
        if not normalized_slug or not pr_id or not holder.strip():
            return None
        now = timezone.now()
        deadline = now + ttl
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                slug=normalized_slug,
                pr_id=pr_id,
                defaults={
                    "mr_url": mr_url,
                    "state": cls.State.REVIEW_DISPATCHED,
                    "holder": holder,
                    "dispatched_at": now,
                    "deadline": deadline,
                },
            )
            if created:
                return row
            acquirable = models.Q(state__in=cls._ACQUIRABLE_STATES) | models.Q(deadline__lt=now)
            claimed = (
                cls.objects.filter(pk=row.pk)
                .filter(acquirable)
                .update(
                    state=cls.State.REVIEW_DISPATCHED,
                    holder=holder,
                    mr_url=mr_url or models.F("mr_url"),
                    dispatched_at=now,
                    deadline=deadline,
                    resolved_at=None,
                )
            )
        if not claimed:
            return None
        row.refresh_from_db()
        return row

    @classmethod
    def acquire_by_url(cls, mr_url: str, *, holder: str, ttl: dt.timedelta = DEFAULT_LOCK_TTL) -> "MRReviewLock | None":
        """:meth:`acquire` for callers that only have the MR/PR web URL (the manual dispatch path)."""
        ref = pr_ref_from_url(mr_url)
        if ref is None:
            msg = f"acquire_by_url: {mr_url!r} is not a recognised PR/MR web URL"
            raise ValueError(msg)
        return cls.acquire(slug=ref.slug, pr_id=ref.number, holder=holder, mr_url=mr_url, ttl=ttl)

    @classmethod
    def mark_verdict_pending(cls, *, slug: str, pr_id: int) -> bool:
        """Advance ``review_dispatched`` -> ``verdict_pending`` for ``(slug, pr_id)``.

        Returns ``True`` iff a row was transitioned. A no-op (returns
        ``False``) when no row is held in ``review_dispatched`` — already
        ``verdict_pending``, resolved, idle, or absent.
        """
        updated = cls.objects.filter(slug=slug.strip(), pr_id=pr_id, state=cls.State.REVIEW_DISPATCHED).update(
            state=cls.State.VERDICT_PENDING
        )
        return bool(updated)

    @classmethod
    def resolve(cls, *, slug: str, pr_id: int) -> bool:
        """Transition the held lock for ``(slug, pr_id)`` to ``resolved``.

        Called when a :class:`~teatree.core.models.review_verdict.ReviewVerdict`
        is recorded for the PR — merge_safe or hold, either way the review
        concluded and the MR is no longer "a review is in flight". Returns
        ``True`` iff a row was transitioned; ``False`` when no row is held
        (already resolved, idle, or absent — never an error, since resolving
        an unheld MR is a legitimate no-op).
        """
        updated = (
            cls.objects.filter(slug=slug.strip(), pr_id=pr_id)
            .filter(state__in=cls._ACTIVE_STATES)
            .update(state=cls.State.RESOLVED, resolved_at=timezone.now())
        )
        return bool(updated)

    @classmethod
    def reconcile_stale(cls, *, at: "dt.datetime | None" = None) -> int:
        """Reset every held row whose ``deadline`` has passed back to ``idle``.

        The explicit reconciler sweep: a dispatched reviewer that died
        without ever recording a verdict leaves its lock ``review_dispatched``
        / ``verdict_pending`` forever unless something resets it. Returns the
        count of rows reset. Self-healing already makes a stale lock
        acquirable and non-blocking for merges at read time (see
        :meth:`acquire` / :meth:`active_lock_for`); this sweep is the
        housekeeping pass that makes the DB state match that reality instead
        of leaving a textually-stale ``review_dispatched`` row sitting around.
        """
        now = at or timezone.now()
        return cls.objects.filter(state__in=cls._ACTIVE_STATES, deadline__lt=now).update(
            state=cls.State.IDLE,
            holder="",
            dispatched_at=None,
            deadline=None,
            resolved_at=None,
        )

    @classmethod
    def active_lock_for(cls, *, slug: str, pr_id: int) -> "MRReviewLock | None":
        """The currently-held (non-stale) lock row for ``(slug, pr_id)``, or ``None``.

        The merge decision point's consult: ``None`` means "no review in
        flight — merge may proceed" (there is no row, the row is
        idle/resolved, or the row's deadline has passed).
        """
        row = cls.objects.filter(slug=slug.strip(), pr_id=pr_id).first()
        if row is not None and row.is_locked():
            return row
        return None

    def is_locked(self, *, at: "dt.datetime | None" = None) -> bool:
        """True iff this row is currently held: an active state with a live deadline."""
        if self.state not in self._ACTIVE_STATES:
            return False
        now = at or timezone.now()
        return self.deadline is None or self.deadline >= now
