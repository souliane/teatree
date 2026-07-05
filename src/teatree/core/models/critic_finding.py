"""Durable per-rubric-item finding the autonomous user-proxy critic records (SELFCATCH-5).

The critic runs its rubric at the FSM's final done-claim (``mark_delivered``) and
writes one ``CriticFinding`` row per rubric item that FAILs its predicate over the
ticket's delivered artifacts. A finding is the durable evidence a delivery
exhibited a failure class the human had to point out this session (believe-done-
not-done, thin plan, ignored input, silent scope reduction, …). Recording is
UNCONDITIONAL — the critic gathers evidence even while it ships advisory (dark) —
so ``critic_catch_rate`` (deferred) can later score the rubric against real defect
escapes, and so a human reading ``workspace doctor`` sees exactly what the critic
saw.

The row is the read-side sibling of :class:`~teatree.core.models.review_verdict.ReviewVerdict`
— a per-item structured judgment keyed by ``(ticket, transition, head_sha)`` — but
this one is written by the deterministic rubric, not an LLM reviewer, so there is
no maker≠checker actor to validate: the predicates are pure functions over durable
state. The ``status`` distinguishes a genuine FAIL from an ``instrumentation_gap``
(the critic could not run the predicate — inconclusive), which the plan's
anti-theater doctrine counts as a FAIL, never a silent pass.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class CriticFinding(models.Model):
    """One recorded critic finding: a rubric item that failed over a ticket's delivery.

    Keyed for lookup by ``(ticket, transition, rubric_item)`` — a re-run of the
    critic at the same transition upserts rather than duplicating, so the row set
    reflects the LATEST verdict per item, not an append-only history. ``detail``
    names the offending artifact so the finding is dispatchable to a fix.
    """

    class Status(models.TextChoices):
        FAIL = "fail", "Fail"
        INSTRUMENTATION_GAP = "instrumentation_gap", "Instrumentation gap"

    ticket = models.ForeignKey(
        "core.Ticket",
        on_delete=models.CASCADE,
        related_name="critic_findings",
    )
    transition = models.CharField(max_length=64)
    rubric_item = models.CharField(max_length=64)
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.FAIL)
    adversarial_question = models.CharField(max_length=255, blank=True, default="")
    detail = models.TextField(blank=True, default="")
    head_sha = models.CharField(max_length=64, blank=True, default="")
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_critic_finding"
        ordering: ClassVar = ["-recorded_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["ticket", "transition", "rubric_item"],
                name="uniq_critic_finding_ticket_transition_item",
            ),
        ]

    def __str__(self) -> str:
        return f"critic-finding<ticket:{self.ticket_id} {self.transition}/{self.rubric_item} {self.status}>"  # type: ignore[attr-defined]  # Django FK accessor

    @classmethod
    def record(cls, *, ticket: "Ticket", transition: str, spec: "CriticFindingSpec") -> "CriticFinding":
        """Upsert the finding for ``(ticket, transition, spec.rubric_item)`` — the single write path.

        Idempotent on the key so a re-run of the critic at the same transition
        overwrites the prior row's detail/status rather than stacking duplicates.
        The per-item content is bundled in *spec* (the ``MergeClear.ClearRequest``
        params-object idiom) so the write contract stays a single argument.
        """
        row, _ = cls.objects.update_or_create(
            ticket=ticket,
            transition=transition,
            rubric_item=spec.rubric_item,
            defaults={
                "status": spec.status,
                "detail": spec.detail,
                "adversarial_question": spec.adversarial_question,
                "head_sha": spec.head_sha,
                "recorded_at": timezone.now(),
            },
        )
        return row


@dataclass(frozen=True, slots=True)
class CriticFindingSpec:
    """The per-item content of one finding — the params object :meth:`CriticFinding.record` takes.

    Bundles the four content fields (plus the informational head SHA) so the write
    contract is one argument, mirroring ``MergeClear.ClearRequest``. ``status``
    defaults to a genuine FAIL; the gate sets ``INSTRUMENTATION_GAP`` for an
    inconclusive predicate.
    """

    rubric_item: str
    detail: str
    status: str = CriticFinding.Status.FAIL
    adversarial_question: str = ""
    head_sha: str = ""
