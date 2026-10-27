"""One durable plan and outcome per ticket round and subject head."""

from typing import TYPE_CHECKING, ClassVar, TypedDict

from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


class ReviewRoundDetail(TypedDict, total=False):
    case: str
    reason: str
    prior_head: str
    patch: str
    delta_files: list[str]
    pr_files: list[str]
    base_ref: str
    carried: list[int]


class RoundMetricManager(models.Manager["RoundMetric"]):
    def open(self, *, kind: str, ticket: "Ticket", subject: str, size: int, detail: ReviewRoundDetail) -> "RoundMetric":
        with transaction.atomic():
            type(ticket).objects.select_for_update().get(pk=ticket.pk)
            existing = self.filter(kind=kind, ticket=ticket, subject=subject).first()
            if existing is not None:
                return existing
            number = self.filter(kind=kind, ticket=ticket).count() + 1
            return self.create(kind=kind, ticket=ticket, subject=subject, round_number=number, size=size, detail=detail)

    def settle(self, *, kind: str, ticket: "Ticket", subject: str, outcome: str) -> int:
        return self.filter(kind=kind, ticket=ticket, subject=subject).update(outcome=outcome)

    def rounds_per_ticket(self, kind: str) -> dict[int, int]:
        return {
            row["ticket_id"]: row["max_round"]
            for row in self.filter(kind=kind).values("ticket_id").annotate(max_round=Max("round_number"))
        }


class RoundMetric(models.Model):
    class Kind(models.TextChoices):
        REVIEW = "review", "Review"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="round_metrics")
    subject = models.CharField(max_length=64)
    round_number = models.PositiveIntegerField()
    size = models.PositiveIntegerField(default=0)
    outcome = models.CharField(max_length=32, blank=True, default="")
    detail = models.JSONField(default=dict)
    recorded_at = models.DateTimeField(default=timezone.now)

    objects = RoundMetricManager()

    class Meta:
        db_table = "teatree_round_metric"
        constraints: ClassVar = [
            models.UniqueConstraint(fields=("kind", "ticket", "subject"), name="uniq_round_kind_ticket_subject")
        ]

    def __str__(self) -> str:
        return f"round<{self.kind}:{self.ticket.pk}:{self.subject[:8]} #{self.round_number}>"
