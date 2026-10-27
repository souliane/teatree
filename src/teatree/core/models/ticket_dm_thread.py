from typing import TYPE_CHECKING, ClassVar

from django.db import models


class TicketDmThread(models.Model):
    ticket = models.OneToOneField("core.Ticket", on_delete=models.CASCADE, related_name="dm_thread")
    channel = models.CharField(max_length=64, blank=True, default="")
    root_ts = models.CharField(max_length=64, blank=True, default="")
    generation = models.PositiveIntegerField(default=0)
    if TYPE_CHECKING:
        ticket_id: int

    class Meta:
        db_table = "teatree_ticket_dm_thread"

    def __str__(self) -> str:
        return f"{self.ticket_id}:{self.generation}"


class TicketDmPost(models.Model):
    thread = models.ForeignKey(TicketDmThread, on_delete=models.CASCADE, related_name="posts")
    generation = models.PositiveIntegerField()
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    question = models.ForeignKey("core.DeferredQuestion", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    if TYPE_CHECKING:
        question_id: int | None

    class Meta:
        db_table = "teatree_ticket_dm_post"
        constraints: ClassVar = [models.UniqueConstraint(fields=["channel", "slack_ts"], name="uniq_ticket_dm_post")]

    def __str__(self) -> str:
        return f"{self.channel}/{self.slack_ts}"
