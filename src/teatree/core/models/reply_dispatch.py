from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.incoming_event import IncomingEvent


class ReplyDispatch(models.Model):
    """Audit row for one outbound message published in response to an event.

    Every ``Replier`` call records a row here keyed by the
    ``(event, target_ref, action_name)`` triple so that a replayed event
    (Slack retry, dispatcher re-run) does not double-post. Status is the
    outcome of the underlying API call.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    event = models.ForeignKey(
        IncomingEvent,
        on_delete=models.CASCADE,
        related_name="dispatches",
    )
    target_ref = models.CharField(max_length=255)
    action_name = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(blank=True)
    dispatched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_reply_dispatch"
        ordering: ClassVar = ["-dispatched_at"]
        indexes: ClassVar = [
            models.Index(fields=["status", "dispatched_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action_name}->{self.target_ref}({self.status})"
