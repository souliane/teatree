from pathlib import Path
from typing import cast

from django.db import models
from django_fsm import FSMField, transition

from teatree.config import load_config as _load_config
from teatree.core.managers import WorktreeManager
from teatree.core.models.ticket import Ticket
from teatree.core.models.types import WorktreeExtra, validated_worktree_extra


def _workspace_dir() -> Path:
    return _load_config().user.workspace_dir


class Worktree(models.Model):
    class State(models.TextChoices):
        CREATED = "created", "Created"
        PROVISIONED = "provisioned", "Provisioned"
        SERVICES_UP = "services_up", "Services up"
        READY = "ready", "Ready"

    overlay = models.CharField(max_length=255)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="worktrees")
    repo_path = models.CharField(max_length=500)
    branch = models.CharField(max_length=255)
    state = FSMField(max_length=32, choices=State.choices, default=State.CREATED)
    db_name = models.CharField(max_length=255, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    objects = WorktreeManager()

    class Meta:
        db_table = "teatree_worktree"

    def __str__(self) -> str:
        return str(self.repo_path)

    @transition(field=state, source=State.CREATED, target=State.PROVISIONED)
    def provision(self) -> None:
        self.db_name = self._build_db_name()

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP, State.READY], target=State.SERVICES_UP)
    def start_services(self, *, services: list[str] | None = None) -> None:
        if services is not None:
            extra = self._extra()
            extra["services"] = services
            self.extra = extra

    @transition(field=state, source=State.SERVICES_UP, target=State.READY)
    def verify(self, *, urls: dict[str, str] | None = None) -> None:
        extra = self._extra()
        if urls:
            extra["urls"] = urls
        self.extra = extra

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP, State.READY], target=State.PROVISIONED)
    def db_refresh(self) -> None:
        from django.utils import timezone  # noqa: PLC0415

        extra = self._extra()
        extra["db_refreshed_at"] = timezone.now().isoformat()
        self.extra = extra

    @transition(field=state, source="*", target=State.CREATED)
    def teardown(self) -> None:
        self.db_name = ""
        self.extra = {}

    def _build_db_name(self) -> str:
        ticket = cast("Ticket", self.ticket)
        variant_suffix = f"_{ticket.variant}" if ticket.variant else ""
        return f"wt_{ticket.ticket_number}{variant_suffix}"

    def get_extra(self) -> WorktreeExtra:
        return validated_worktree_extra(self.extra)

    def _extra(self) -> WorktreeExtra:
        return validated_worktree_extra(self.extra)
