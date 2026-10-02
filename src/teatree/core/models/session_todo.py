"""A durable per-session working TODO, hanging off the generic :class:`Session`.

Directive #22 / souliane/teatree#3572. A long-lived entrypoint/orchestrator
session juggles many in-flight threads. In foreground Claude Code it has the
harness TODO panel; in background/headless it has **no TODO tool at all**, so it
renders its list as inline chat text and silently forgets in-flight work across
turns and compaction.

The factory ``Task`` queue does not fill that gap — it is headless *work* the
loop dispatches, a different concern from a session's own scratch list. So this
is a third thing: a working list keyed on ``Session``, which is already
harness-agnostic by construction (``agent_id`` is a plain string, ``overlay`` a
name, no Claude field anywhere on it). Any harness reads and writes the same
rows through ``t3 <overlay> session todo …``.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.session import Session


class SessionTodoManager(models.Manager["SessionTodo"]):
    """Read + mutate surface for one session's working list."""

    def open_for(self, session: Session) -> models.QuerySet["SessionTodo"]:
        """The items still needing attention, in working order."""
        return self.filter(session=session).exclude(status=SessionTodo.Status.DONE)

    def add(self, session: Session, text: str) -> "SessionTodo":
        """Append *text* to *session*'s list at the end of the current order."""
        last = self.filter(session=session).aggregate(models.Max("order"))["order__max"]
        return self.create(session=session, text=text, order=(last or 0) + 1)


class SessionTodo(models.Model):
    """One working-list item belonging to one session."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        DONE = "done", "Done"

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="todos")
    text = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SessionTodoManager()

    class Meta:
        db_table = "teatree_session_todo"
        ordering: ClassVar[list[str]] = ["order", "pk"]

    def __str__(self) -> str:
        return f"[{self.status}] {self.text}"

    def set_status(self, status: "SessionTodo.Status") -> None:
        """Move the item to *status* and persist it."""
        self.status = status
        self.updated_at = timezone.now()
        self.save(update_fields=["status", "updated_at"])
