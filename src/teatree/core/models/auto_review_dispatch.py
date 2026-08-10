"""Idempotency ledger + claimable-task factory for auto-review dispatch (#68).

When ``PrSweepScanner`` finds an OWN, CI-green, mergeable, non-draft PR on a
full-autonomy overlay with no recorded independent cold-review, it cannot
merge — the maker≠checker boundary forbids self-attestation. Pre-#68 the
scanner only logged ``flag_no_review`` and the PR waited for a human to notice.

This model closes that loop: ``enqueue`` records a row keyed on
``(slug, pr_id, head_sha)`` and creates the claimable ``Task(phase=reviewing)``
the loop self-pump dispatches to ``t3:reviewer``. The reviewer cold-reviews the
PR and records a ``merge_safe`` :class:`ReviewVerdict` bound to the reviewed
head; recording the verdict triggers the sweep merge on demand
(``teatree.loop.sweep_on_demand``, #2026) instead of waiting a full tick
cadence, and the periodic sweep consumes the same verdict
(``_has_independent_cold_review``) as the backstop. Dedup is per ``(PR, head_sha)``: a new push (new head) re-arms
exactly one new task; an open task for the same head never duplicates; a
recorded verdict for the head suppresses enqueue upstream (the PR never reaches
``flag_no_review``).

The claim carries a ``deadline`` and a terminal ``state``, in the shape
:class:`~teatree.core.models.mr_review_lock.MRReviewLock` established: a
dispatched review that ends without recording a verdict expires, and the next
sweep re-arms that head instead of leaving it permanently unmergeable (#3920).
``attempts`` bounds the retry at :data:`MAX_DISPATCH_ATTEMPTS`, so a review that
can never succeed saturates and is surfaced by the doctor's reconciliation
ledger rather than re-armed forever. ``mark_resolved`` is the terminal, fired
when a :class:`~teatree.core.models.review_verdict.ReviewVerdict` lands for the
head.

Mirrors :class:`teatree.core.models.red_mr_fix_attempt.RedMrFixAttempt`
(idempotent claim keyed on ``(pr_url, head_sha)``).

Also acquires the per-MR :class:`~teatree.core.models.mr_review_lock.MRReviewLock`
(#1405) before dispatching: a PR that gets a new push while its PRIOR review is
still in flight (dispatched, not yet resolved) no longer arms a second,
concurrent reviewer for the new head — the lock is per-``(slug, pr_id)``, not
per-head, so it also dedups ACROSS heads, closing the gap where a fresh push
during an in-flight review used to arm a second reviewer.
"""

import datetime as dt
from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.expiring_claim import acquirable_q
from teatree.core.modelkit.review_contract import ENVELOPE_FINDINGS_RULE
from teatree.core.models.mr_review_lock import DEFAULT_LOCK_TTL, MRReviewLock

if TYPE_CHECKING:
    from teatree.core.models.task import Task

#: The holder identity the loop scanner takes the per-MR review lock under. A
#: verdict recorded for a head this dispatch armed releases that lock; a verdict
#: from any other path does not (see :meth:`MRReviewLock.resolve`).
LOOP_SCANNER_HOLDER = "loop-scanner:auto-review-dispatch"

#: How long an armed review may hold its claim before the head is re-armable.
#: Equal to the review lock's TTL on purpose: the dispatch and the lock are
#: claimed together and must expire together, or the re-arm would be refused by
#: a lock that is still nominally held.
DEFAULT_DISPATCH_TTL: dt.timedelta = DEFAULT_LOCK_TTL

#: How many times one head may be armed before the claim is left saturated. A
#: review that dies three times is not going to succeed on the fourth; re-arming
#: forever would burn the agent lane on a PR that needs a human. Saturation is
#: surfaced by ``t3 doctor check`` rather than left silent.
MAX_DISPATCH_ATTEMPTS = 3


def build_review_contract(*, slug: str, pr_id: int, head_sha: str, pr_url: str) -> str:
    """The reviewer's standing contract, stamped into the task's execution_reason.

    The dispatched ``t3:reviewer`` cold-reviews the diff per /t3:review doctrine
    and RETURNS its verdict in the ``review_verdict`` result envelope. The phase
    carries the shell (``phase_tools.VERDICT_REVIEW_PHASES``, #3549), so the contract
    spends it: the verify-or-fail checkout and the diff-shape audit are named here
    rather than left for the reviewer to improvise. The ``t3 <overlay> review record``
    ban survives on its own merit — maker≠checker requires a different actor to write
    the row, so the orchestrator records the ``ReviewVerdict`` server-side from the
    returned envelope, which is the artifact the next pr_sweep merges on and which
    releases the review lock (#68, #1405).
    """
    return (
        f"Cold-review the diff of {slug}#{pr_id} ({pr_url}) per /t3:review doctrine at head "
        f"{head_sha[:8]}. You have the shell: check the reviewed head out with "
        f"`t3 review checkout {pr_url} --sha {head_sha}` (verify-or-fail; never a raw "
        f"`git worktree add <branch>`), audit the diff shape with `t3 review run {pr_url}`, and run the "
        f"affected tests in that checkout before voting merge_safe. Then RETURN your verdict in the "
        f'result envelope: `"review_verdict": '
        f'{{"verdict": "merge_safe", "reviewed_sha": "{head_sha}", "reviewer_identity": '
        f'"<your-reviewer-id>", "gh_verify_result": "green", "findings": [{{"severity": "low", '
        f'"summary": "<what you observed>", "file": "<path>", "line": 0}}]}}`. {ENVELOPE_FINDINGS_RULE} '
        f"Do NOT run `t3 <overlay> review record` — maker≠checker requires a different "
        f"actor to write the row: the orchestrator records the ReviewVerdict at head {head_sha[:8]} from "
        f"your envelope, and pr_sweep consumes it to auto-merge this own PR (#68)."
    )


class AutoReviewDispatch(models.Model):
    """One auto-review dispatch for a PR head SHA — the dedup key + task link.

    The unique key ``(slug, pr_id, head_sha)`` deduplicates re-ticks: a second
    sweep on the same head returns the existing row and enqueues no new task.
    A new push (new head SHA) records a fresh row and arms exactly one new
    reviewing task. The ``task`` FK links the dispatch to the claimable
    ``Task(phase=reviewing)`` so the row is a faithful record of what was
    enqueued.
    """

    class State(models.TextChoices):
        DISPATCHED = "dispatched", "Dispatched"
        RESOLVED = "resolved", "Resolved"

    #: In-flight: acquirable again only once ``deadline`` has passed.
    _ACTIVE_STATES: ClassVar[frozenset[str]] = frozenset({State.DISPATCHED})
    #: Empty on purpose — unlike the per-MR lock, a RESOLVED per-head claim is
    #: terminal: a verdict already covers that exact tree.
    _ACQUIRABLE_STATES: ClassVar[frozenset[str]] = frozenset()

    slug = models.CharField(max_length=255)
    pr_id = models.IntegerField()
    head_sha = models.CharField(max_length=64)
    pr_url = models.URLField(max_length=512, blank=True, default="")
    overlay = models.CharField(max_length=64, blank=True, default="")
    task = models.ForeignKey(
        "core.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="auto_review_dispatches",
    )
    state = models.CharField(max_length=32, choices=State.choices, default=State.DISPATCHED)
    deadline = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=1)
    dispatched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_auto_review_dispatch"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["slug", "pr_id", "head_sha"],
                name="uniq_auto_review_slug_pr_head",
            ),
        ]

    def __str__(self) -> str:
        return f"auto-review<{self.pk}:{self.slug}#{self.pr_id}@{self.head_sha[:8]}>"

    @classmethod
    def saturated(cls, *, at: "dt.datetime | None" = None) -> models.QuerySet:
        """Claims that spent their whole retry budget and still hold no verdict.

        The visible end of the bounded retry: every attempt was armed and none
        produced a verdict, so nothing will re-arm this head and it needs a
        human. Surfaced by the doctor's reconciliation ledger.

        Precisely :meth:`_reclaim`'s claim test with the budget inverted, over
        the same :func:`acquirable_q` predicate: a saturated claim is one the
        reclaim would take but for its exhausted ``attempts``. Spelling the
        state/deadline half inline here instead would let "will not re-arm"
        drift from "would re-arm", which is the whole point of the shared rule.
        """
        now = at or timezone.now()
        return cls.objects.filter(
            acquirable_q(always_acquirable=cls._ACQUIRABLE_STATES, active=cls._ACTIVE_STATES, now=now),
            attempts__gte=MAX_DISPATCH_ATTEMPTS,
        )

    @classmethod
    def mark_resolved(cls, *, slug: str, pr_id: int, head_sha: str) -> bool:
        """Terminal: a verdict landed for this exact head, so the claim is spent.

        Returns ``True`` iff a row transitioned. Called when a
        :class:`~teatree.core.models.review_verdict.ReviewVerdict` is recorded —
        merge_safe or hold, either way the review concluded. Resolving an
        unclaimed head is a legitimate no-op, never an error.
        """
        return bool(
            cls.objects.filter(slug=slug.strip(), pr_id=pr_id, head_sha=head_sha.strip().lower())
            .filter(state__in=cls._ACTIVE_STATES)
            .update(state=cls.State.RESOLVED, resolved_at=timezone.now())
        )

    @classmethod
    def enqueue(
        cls,
        *,
        slug: str,
        pr_id: int,
        head_sha: str,
        pr_url: str = "",
        overlay: str = "",
    ) -> "AutoReviewDispatch | None":
        """Record the dispatch + create one claimable reviewing Task — idempotently.

        Returns the row (carrying the enqueued task) when a review was armed;
        ``None`` when it was not. A claim is armed on the first dispatch for
        ``(slug, pr_id, head_sha)``, and re-armed when an existing claim has
        EXPIRED without producing a verdict and has retry budget left — the
        dispatched reviewer died, so the head is un-reviewed and must not stay
        un-armable (#3920). ``None`` covers the three refusals: a live claim
        (a review really is in flight), a RESOLVED claim (a verdict already
        covers this exact tree), and a saturated one
        (:data:`MAX_DISPATCH_ATTEMPTS` reviews died — see :meth:`saturated`).

        ``None`` also when the per-MR
        :class:`~teatree.core.models.mr_review_lock.MRReviewLock` is held for
        ``(slug, pr_id)`` — a review for a DIFFERENT (older) head on the same MR
        is still in flight (#1405: the lock is keyed on the MR, not the head, so
        it also dedups a fresh push arriving while the prior review hasn't
        concluded). The claim, the lock and the Task share one transaction, so a
        refused lock unwinds the claim instead of leaving a half-claimed head
        that would dedup the next tick out of arming a review nobody dispatched.
        """
        if not slug or not head_sha:
            return None
        now = timezone.now()
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                slug=slug,
                pr_id=pr_id,
                head_sha=head_sha,
                defaults={
                    "pr_url": pr_url,
                    "overlay": overlay,
                    "state": cls.State.DISPATCHED,
                    "deadline": now + DEFAULT_DISPATCH_TTL,
                    "attempts": 1,
                },
            )
            if not created and not cls._reclaim(row, now=now):
                return None
            if MRReviewLock.acquire(slug=slug, pr_id=pr_id, holder=LOOP_SCANNER_HOLDER, mr_url=pr_url) is None:
                # Another reviewer holds the MR. Unwind the claim rather than
                # strand it: a half-claimed head would dedup the next tick out of
                # arming a review that was never dispatched.
                transaction.set_rollback(True)
                return None
            row.refresh_from_db()
            row.task = cls._create_reviewing_task(
                slug=slug,
                pr_id=pr_id,
                head_sha=head_sha,
                pr_url=pr_url,
                overlay=overlay,
            )
            row.save(update_fields=["task"])
        return row

    @classmethod
    def _reclaim(cls, row: "AutoReviewDispatch", *, now: dt.datetime) -> bool:
        """Compare-and-set an EXPIRED, unresolved claim back to dispatched. True iff re-armed.

        The same shape :meth:`MRReviewLock.acquire` uses, over the shared
        :func:`acquirable_q` predicate: a single conditional ``UPDATE`` is the
        atomic claim, so two concurrent ticks racing the same stranded head
        cannot both arm a reviewer. The retry budget is part of the condition
        rather than a separate read, so exhausting it is the same atomic step.
        """
        return bool(
            cls.objects.filter(pk=row.pk)
            .filter(
                acquirable_q(
                    always_acquirable=cls._ACQUIRABLE_STATES,
                    active=cls._ACTIVE_STATES,
                    now=now,
                )
            )
            .filter(attempts__lt=MAX_DISPATCH_ATTEMPTS)
            .update(
                state=cls.State.DISPATCHED,
                deadline=now + DEFAULT_DISPATCH_TTL,
                attempts=models.F("attempts") + 1,
                resolved_at=None,
            )
        )

    @staticmethod
    def _create_reviewing_task(*, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> "Task":
        from teatree.core.models.session import Session  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

        ticket, _ = Ticket.objects.get_or_create(
            issue_url=pr_url or f"{slug}#{pr_id}",
            defaults={"overlay": overlay, "role": Ticket.Role.REVIEWER},
        )
        session = Session.objects.create(ticket=ticket, agent_id="auto-review-dispatch")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_reason=build_review_contract(slug=slug, pr_id=pr_id, head_sha=head_sha, pr_url=pr_url),
        )
