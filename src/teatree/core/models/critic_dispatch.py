"""Idempotency ledger + claimable-task factory for the async user-proxy critic (SELFCATCH-5).

The mirror of :class:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch`
for the critic: when ``mark_delivered`` fires and no fresh
:class:`~teatree.core.models.critic_verdict.CriticVerdict` covers the delivered
head, the gate calls :meth:`enqueue` to record a row keyed on
``(ticket, transition, head_sha)`` and create the claimable headless
``Task(phase="critic_reviewing")`` the loop self-pump dispatches. The critic reads the
delivered artifacts and RETURNS a ``critic_verdict`` envelope; ``attempt_recorder``
records the ``CriticVerdict`` server-side (maker≠checker — a different actor writes
it), and the gate mirrors its FAIL items into ``CriticFinding``.

Dedup is per ``(ticket, transition, head_sha)``: a re-fire at the same delivered
head returns the existing row and enqueues no second critic; a new head arms
exactly one fresh critic. The row insert and the ``Task`` creation share one
transaction so a row never exists without its task.

The claim carries a ``deadline``, a terminal ``state`` and a bounded ``attempts``
count, the same shape :class:`~teatree.core.models.mr_review_lock.MRReviewLock`
established and :class:`~teatree.core.models.auto_review_dispatch.AutoReviewDispatch`
shares: a critic task that dies without recording a verdict expires and is
re-armed, rather than leaving the merge-quality gate unsatisfiable for that head
forever (#3920).

The task belongs to the ticket under judgment (not a synthetic reviewer ticket):
the critic's subject IS this delivery. The gate composes the rubric-injected
``contract`` (the model stays free of any ``critic_rubric`` import — no model→gate
up-edge); the loop dispatches the headless task by phase.
"""

import datetime as dt
from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.expiring_claim import acquirable_q
from teatree.core.models.auto_review_dispatch import DEFAULT_DISPATCH_TTL, MAX_DISPATCH_ATTEMPTS
from teatree.core.models.session import Session
from teatree.core.models.task import Task

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class CriticDispatch(models.Model):
    """One async critic dispatch for a delivered head — the dedup key + task link."""

    class State(models.TextChoices):
        DISPATCHED = "dispatched", "Dispatched"
        RESOLVED = "resolved", "Resolved"

    #: In-flight: acquirable again only once ``deadline`` has passed.
    _ACTIVE_STATES: ClassVar[frozenset[str]] = frozenset({State.DISPATCHED})
    #: Empty on purpose — a RESOLVED per-head claim is terminal: a CriticVerdict
    #: already covers that exact delivered tree.
    _ACQUIRABLE_STATES: ClassVar[frozenset[str]] = frozenset()

    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="critic_dispatches")
    transition = models.CharField(max_length=64)
    head_sha = models.CharField(max_length=64, blank=True, default="")
    task = models.ForeignKey(
        "core.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="critic_dispatches",
    )
    state = models.CharField(max_length=32, choices=State.choices, default=State.DISPATCHED)
    deadline = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=1)
    dispatched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_critic_dispatch"
        ordering: ClassVar = ["-dispatched_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["ticket", "transition", "head_sha"],
                name="uniq_critic_dispatch_ticket_transition_head",
            ),
        ]

    def __str__(self) -> str:
        return f"critic-dispatch<{self.pk}:ticket:{self.ticket_id} {self.transition}@{self.head_sha[:8]}>"  # type: ignore[attr-defined]  # Django FK accessor

    @classmethod
    def enqueue(
        cls,
        *,
        ticket: "Ticket",
        transition: str,
        head_sha: str,
        contract: str,
    ) -> "CriticDispatch | None":
        """Record the dispatch + create one claimable headless critic task — idempotently.

        Returns the row when a critic was armed; ``None`` when it was not. A
        claim is armed on the first dispatch for ``(ticket, transition,
        head_sha)``, and re-armed when an existing claim EXPIRED without
        producing a verdict and has retry budget left — a dead critic task must
        not leave the merge-quality gate unsatisfiable for that head forever
        (#3920). ``None`` covers a live claim, a RESOLVED one (a verdict already
        covers this delivered tree), and a saturated one
        (:data:`MAX_DISPATCH_ATTEMPTS` critics died — see :meth:`saturated`).
        The claim and the ``Task`` share one transaction so a row never exists
        without its task.
        """
        normalized_head = head_sha.strip().lower()
        now = timezone.now()
        with transaction.atomic():
            row, created = cls.objects.get_or_create(
                ticket=ticket,
                transition=transition,
                head_sha=normalized_head,
                defaults={"state": cls.State.DISPATCHED, "deadline": now + DEFAULT_DISPATCH_TTL, "attempts": 1},
            )
            if not created and not cls._reclaim(row, now=now):
                return None
            row.refresh_from_db()
            row.task = cls._create_critic_task(ticket=ticket, contract=contract)
            row.save(update_fields=["task"])
        return row

    @classmethod
    def _reclaim(cls, row: "CriticDispatch", *, now: dt.datetime) -> bool:
        """Compare-and-set an EXPIRED, unresolved claim back to dispatched. True iff re-armed.

        The same shared :func:`acquirable_q` predicate ``MRReviewLock.acquire``
        and ``AutoReviewDispatch`` use — one conditional ``UPDATE`` is the atomic
        claim, so two concurrent ticks cannot both arm a critic for one head.
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
    def mark_resolved(cls, *, ticket: "Ticket", transition: str, head_sha: str) -> bool:
        """Terminal: a :class:`CriticVerdict` landed for this delivered head."""
        return bool(
            cls.objects.filter(ticket=ticket, transition=transition, head_sha=head_sha.strip().lower())
            .filter(state__in=cls._ACTIVE_STATES)
            .update(state=cls.State.RESOLVED, resolved_at=timezone.now())
        )

    @staticmethod
    def _create_critic_task(*, ticket: "Ticket", contract: str) -> "Task":
        session = Session.objects.create(ticket=ticket, agent_id="critic-dispatch")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase="critic_reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason=contract,
        )
