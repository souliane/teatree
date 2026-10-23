import hashlib
import json
from typing import TYPE_CHECKING, ClassVar

from django.db import models

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def slack_command_id(overlay: str, slack_ts: str) -> str:
    payload = json.dumps([overlay, slack_ts], ensure_ascii=False, separators=(",", ":"))
    return f"slack:{hashlib.sha256(payload.encode()).hexdigest()}"


class ThreadBinding(models.Model):
    ticket_id: int
    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    thread_ts = models.CharField(max_length=64)
    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="slack_threads")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "teatree_thread_binding"
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["overlay", "channel", "thread_ts"], name="uniq_thread_binding_origin")
        ]

    def __str__(self) -> str:
        return f"{self.overlay}:{self.channel}:{self.thread_ts} -> ticket {self.ticket_id}"

    @classmethod
    def bind(cls, *, ticket: "Ticket", channel: str, thread_ts: str) -> "ThreadBinding":
        if not channel or not thread_ts:
            msg = "A Slack thread binding needs channel and root timestamp"
            raise ValueError(msg)
        binding, _created = cls.objects.get_or_create(
            overlay=ticket.overlay, channel=channel, thread_ts=thread_ts, defaults={"ticket": ticket}
        )
        if binding.ticket_id != ticket.pk:
            msg = "A Slack thread is already bound to another ticket"
            raise ValueError(msg)
        return binding


class SessionCommand(models.Model):
    ticket_id: int
    task_id: int
    command_id = models.CharField(max_length=128, unique=True)
    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="session_commands")
    task = models.ForeignKey("core.Task", on_delete=models.CASCADE, related_name="session_commands")
    text = models.TextField()
    outcome = models.CharField(max_length=40, blank=True, default="")
    code = models.CharField(max_length=40, blank=True, default="")
    accepted_at = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "teatree_session_command"

    def __str__(self) -> str:
        return f"{self.command_id}: {self.outcome or 'unsettled'}"
