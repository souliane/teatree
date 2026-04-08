from typing import ClassVar

from django.db import models

from teatree.core.models.ticket import Ticket


class TicketTransition(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="transitions")
    session = models.ForeignKey(
        "core.Session",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transitions",
    )
    from_state = models.CharField(max_length=32)
    to_state = models.CharField(max_length=32)
    triggered_by = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "teatree_tickettransition"
        ordering: ClassVar = ["created_at"]

    def __str__(self) -> str:
        return f"{self.from_state} → {self.to_state} ({self.triggered_by})"
