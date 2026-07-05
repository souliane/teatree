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

The task belongs to the ticket under judgment (not a synthetic reviewer ticket):
the critic's subject IS this delivery. The gate composes the rubric-injected
``contract`` (the model stays free of any ``critic_rubric`` import — no model→gate
up-edge); the loop dispatches the headless task by phase.
"""

from typing import TYPE_CHECKING, ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class CriticDispatch(models.Model):
    """One async critic dispatch for a delivered head — the dedup key + task link."""

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
    def enqueue(cls, *, ticket: "Ticket", transition: str, head_sha: str, contract: str) -> "CriticDispatch | None":
        """Record the dispatch + create one claimable headless critic task — idempotently.

        Returns the new row on the first dispatch for ``(ticket, transition,
        head_sha)``; ``None`` when a row for that head already exists (a prior
        tick already armed the critic). The row insert and the ``Task`` creation
        share one transaction so a row never exists without its task.
        """
        normalized_head = head_sha.strip().lower()
        with transaction.atomic():
            row, created = cls.objects.get_or_create(ticket=ticket, transition=transition, head_sha=normalized_head)
            if not created:
                return None
            row.task = cls._create_critic_task(ticket=ticket, contract=contract)
            row.save(update_fields=["task"])
        return row

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
