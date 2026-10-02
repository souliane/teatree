from typing import ClassVar

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from teatree.core.models.incoming_event import IncomingEvent


class IntentClassification(models.Model):
    """Classifier verdict for one ``IncomingEvent``.

    Stored separately from the event row so reclassification (rules
    evolving, LLM fallback firing) leaves an audit trail without
    mutating the canonical event record. Only one classification per
    event in steady state — enforced by the OneToOne shape — but the
    row is mutable so a reclassify pass can overwrite the verdict
    rather than fight a unique constraint.
    """

    class Intent(models.TextChoices):
        TASK = "task", "Task"
        QUESTION = "question", "Question"
        APPROVAL = "approval", "Approval"
        STATUS_UPDATE = "status_update", "Status update"
        ESCALATION = "escalation", "Escalation"
        # A standing behavioral constraint on teatree ITSELF ("always open MRs as
        # drafts for overlay X"), vs TASK = "a piece of work to do now". Unrouteable at
        # intake (#105 deleted ambient detection): the router DROPs a DIRECTIVE event —
        # the only Directive producer is the explicit `Directive.objects.capture` CLI.
        DIRECTIVE = "directive", "Directive"
        NOISE = "noise", "Noise"

    event = models.OneToOneField(
        IncomingEvent,
        on_delete=models.CASCADE,
        related_name="classified_as",
    )
    intent = models.CharField(max_length=32, choices=Intent.choices)
    confidence = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
    )
    rationale = models.CharField(max_length=255, blank=True)
    classified_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_intent_classification"
        ordering: ClassVar = ["-classified_at"]
        indexes: ClassVar = [
            models.Index(fields=["intent", "classified_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.intent}@{self.confidence:.2f}({self.event.pk})"
