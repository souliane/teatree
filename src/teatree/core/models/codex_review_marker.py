"""Expiry-acquirable claim for ``/codex:review`` and self-PR review dispatch (#1254, #3921).

When :class:`CodexReviewScanner` (or the #3569 self-PR scanner) dispatches a review
for a newly-pushed PR head SHA, persistence claims one :class:`CodexReviewMarker`
row keyed on ``(slug, pr_id, head_sha)`` in the same transaction that creates the
reviewer Task. Re-ticking on the same SHA returns no row and the dispatch is
skipped — the fleet-of-agents rule is enforced once per SHA, never on every tick.
A force-push (new SHA) claims a fresh row and re-fires the review.

The claim carries a ``deadline``, a terminal ``state`` and a bounded ``attempts``
count — the shape :class:`~teatree.core.models.mr_review_lock.MRReviewLock`
established and the two dispatch ledgers took on in #3920. This table was the
fourth claim on the review path and the last one left as a bare ``get_or_create``:
a dispatched review that died left its head permanently un-redispatchable, so the
only recovery was a force-push or a hand-run ``t3 codex review --force``. An
expired, unresolved claim is now re-armed instead, and a claim that burns its whole
retry budget is surfaced by the doctor's reconciliation ledger rather than sitting
silent.

``mark_resolved`` is the terminal, fired when a
:class:`~teatree.core.models.review_verdict.ReviewVerdict` lands for the head — the
same event that spends the sibling per-head claim, because it means the head really
was reviewed.

``mark_refused`` is the OTHER terminal (#4522, #4530), and this table is where the re-arm
was actually measured: of the 18 review runs that hit the checks-contradiction refusal on
this deploy, 11 were armed by THIS claim. A reviewer returns ``merge_safe`` over required
checks the same reviewer reported RED; recording is refused, nothing lands, the head keeps
no verdict, and ``claim`` re-arms it once the ``deadline`` lapses.

The latch fires only at :data:`MAX_DISPATCH_ATTEMPTS`, never on the first refusal. The
refusal compares two fields of ONE envelope and so describes that reviewer, not the tree:
6 of the 9 heads that ever hit it recorded a verdict at the SAME head afterwards, three of
them a ``hold`` over checks that genuinely were red. Latching early buys back runs at the
price of those recoveries. At the bound the claim is spent either way, so the latch costs
nothing and adds the one thing saturation cannot say — WHY.

Neither this terminal nor its twin frees an
:class:`~teatree.core.models.mr_review_lock.MRReviewLock`. This path never takes one, and a
refusal — which records nothing a merge can consume — must not free a lock some OTHER,
still-running reviewer holds. A concluded ``ReviewVerdict`` releases it because a merge
guard is about to consume that verdict and a held lock would block the merge it just
authorised; a refusal authorises nothing, so taking the guard away would give nothing back.
"""

import datetime as dt
from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.modelkit.expiring_claim import acquirable_q, retire_head_claim
from teatree.core.models.auto_review_dispatch import DEFAULT_DISPATCH_TTL, MAX_DISPATCH_ATTEMPTS


class CodexReviewMarker(models.Model):
    """One codex / self-PR review dispatch claim for a PR head SHA."""

    class State(models.TextChoices):
        DISPATCHED = "dispatched", "Dispatched"
        RESOLVED = "resolved", "Resolved"
        REFUSED = "refused", "Refused"

    #: In-flight: acquirable again only once ``deadline`` has passed.
    _ACTIVE_STATES: ClassVar[frozenset[str]] = frozenset({State.DISPATCHED})
    #: Empty on purpose — a terminal per-head claim stays terminal. RESOLVED means a
    #: verdict already covers that exact tree, so re-arming it would be review churn;
    #: REFUSED means a verdict for that exact tree is structurally unrecordable and a
    #: human has been paged. Neither is re-armable, for opposite reasons, and both are
    #: escaped the same way — by a new head (#4530).
    _ACQUIRABLE_STATES: ClassVar[frozenset[str]] = frozenset()

    slug = models.CharField(max_length=128)
    pr_id = models.IntegerField()
    head_sha = models.CharField(max_length=64)
    overlay = models.CharField(max_length=64, blank=True, default="")
    variant = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=32, choices=State.choices, default=State.DISPATCHED)
    deadline = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=1)
    dispatched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_codex_review_marker"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["slug", "pr_id", "head_sha"],
                name="uniq_codexreviewmarker_slug_pr_sha",
            ),
        ]

    def __str__(self) -> str:
        return f"codex-review-marker<{self.pk}:{self.slug}#{self.pr_id}@{self.head_sha[:8]}>"

    @classmethod
    def claim(
        cls,
        *,
        slug: str,
        pr_id: int,
        head_sha: str,
        overlay: str = "",
        variant: str = "",
    ) -> "CodexReviewMarker | None":
        """Claim the head idempotently; return the row to dispatch on, or ``None``.

        ``None`` means "do not dispatch a review for this
        ``(slug, pr_id, head_sha)``" and covers three refusals: a LIVE claim (a
        review really is in flight), a RESOLVED one (a verdict already covers this
        exact tree), and a saturated one (:data:`MAX_DISPATCH_ATTEMPTS` reviews died
        — see :meth:`saturated`).

        A claim is taken on the first dispatch for the head, and re-taken when an
        existing claim EXPIRED without producing a verdict and has retry budget
        left: the dispatched review died, so the head is un-reviewed and must not
        stay un-dispatchable (#3921). The re-arm reclaims the existing row rather
        than inserting a second one, so the unique key still holds.
        """
        if not slug or not pr_id or not head_sha:
            return None
        now = timezone.now()
        row, created = cls.objects.get_or_create(
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            defaults={
                "overlay": overlay,
                "variant": variant,
                "state": cls.State.DISPATCHED,
                "deadline": now + DEFAULT_DISPATCH_TTL,
                "attempts": 1,
            },
        )
        if created:
            return row
        if not cls._reclaim(row, now=now):
            return None
        row.refresh_from_db()
        return row

    @classmethod
    def _reclaim(cls, row: "CodexReviewMarker", *, now: dt.datetime) -> bool:
        """Compare-and-set an EXPIRED, unresolved claim back to dispatched. True iff re-armed.

        The same shared :func:`acquirable_q` predicate ``MRReviewLock.acquire`` and
        the two dispatch ledgers use — one conditional ``UPDATE`` is the atomic
        claim, so two concurrent ticks racing the same stranded head cannot both
        dispatch a review. The retry budget is part of the condition rather than a
        separate read, so exhausting it is the same atomic step.
        """
        return bool(
            cls.objects.filter(pk=row.pk)
            .filter(acquirable_q(always_acquirable=cls._ACQUIRABLE_STATES, active=cls._ACTIVE_STATES, now=now))
            .filter(attempts__lt=MAX_DISPATCH_ATTEMPTS)
            .update(
                state=cls.State.DISPATCHED,
                deadline=now + DEFAULT_DISPATCH_TTL,
                attempts=models.F("attempts") + 1,
                resolved_at=None,
            )
        )

    @classmethod
    def saturated(cls, *, at: "dt.datetime | None" = None) -> models.QuerySet:
        """Claims that spent their whole retry budget and still hold no verdict.

        :meth:`_reclaim`'s claim test with the budget inverted, over the same
        :func:`acquirable_q` predicate — the twin of
        :meth:`AutoReviewDispatch.saturated`.
        """
        now = at or timezone.now()
        return cls.objects.filter(
            acquirable_q(always_acquirable=cls._ACQUIRABLE_STATES, active=cls._ACTIVE_STATES, now=now),
            attempts__gte=MAX_DISPATCH_ATTEMPTS,
        )

    @classmethod
    def mark_resolved(cls, *, slug: str, pr_id: int, head_sha: str) -> bool:
        """Terminal: a verdict landed for this exact head, so the claim is spent.

        Returns ``True`` iff a row transitioned. Resolving an unclaimed head is a
        legitimate no-op — most verdicts conclude a review this table never armed.
        """
        return retire_head_claim(
            cls.objects.filter(state__in=cls._ACTIVE_STATES),
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            to_state=cls.State.RESOLVED,
        )

    @classmethod
    def mark_refused(cls, *, slug: str, pr_id: int, head_sha: str) -> bool:
        """Terminal for a head whose retry budget ran out on an unrecordable verdict.

        Returns ``True`` iff a row transitioned. The twin of
        :meth:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch.mark_refused`
        and now byte-for-byte the same rule, which is the #4530 correction: neither frees a
        per-MR lock, and each latches ONLY its own row, only at
        :data:`MAX_DISPATCH_ATTEMPTS`. This table matters because it is what :meth:`claim`
        re-acquires after the ``deadline`` on the codex / self-PR path — the path that armed
        11 of the 18 measured refusal runs.

        Below the bound this is a deliberate no-op: the refusal describes one reviewer's
        self-contradictory envelope, not the tree, and the head recovers on a later attempt
        more often than not. Refusing an unclaimed head is a no-op too, never an error.
        """
        return retire_head_claim(
            cls.objects.filter(state__in=cls._ACTIVE_STATES, attempts__gte=MAX_DISPATCH_ATTEMPTS),
            slug=slug,
            pr_id=pr_id,
            head_sha=head_sha,
            to_state=cls.State.REFUSED,
        )
