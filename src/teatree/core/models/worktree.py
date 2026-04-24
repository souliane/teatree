from pathlib import Path
from typing import ClassVar, cast

from django.db import models, transaction
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

    @transition(field=state, source=[State.CREATED, State.PROVISIONED], target=State.PROVISIONED)
    def provision(self) -> None:
        """Schedule heavy provisioning side-effects.

        Pure transition body (BLUEPRINT §4): state + ``db_name`` here, then
        ``execute_worktree_provision`` enqueued after commit so the env
        cache, direnv + prek setup, DB import, overlay steps and health
        checks all run in a worker. Source ``[CREATED, PROVISIONED]`` makes
        re-firing idempotent — a previous worker that crashed mid-import
        can be retried without going back to CREATED.
        """
        from teatree.core.worktree_tasks import execute_worktree_provision  # noqa: PLC0415

        self.db_name = self._build_db_name()
        worktree_pk = int(self.pk)
        transaction.on_commit(lambda: execute_worktree_provision.enqueue(worktree_pk))

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP, State.READY], target=State.SERVICES_UP)
    def start_services(self, *, services: list[str] | None = None) -> None:
        """Schedule docker compose up.

        Pure transition body (BLUEPRINT §4): record the intended services,
        then ``execute_worktree_start`` enqueued after commit drives the
        actual ``docker compose up``. Source allows re-firing from
        SERVICES_UP / READY so a partially-failed boot can be retried.
        """
        from teatree.core.worktree_tasks import execute_worktree_start  # noqa: PLC0415

        if services is not None:
            extra = self._extra()
            extra["services"] = services
            self.extra = extra
        worktree_pk = int(self.pk)
        transaction.on_commit(lambda: execute_worktree_start.enqueue(worktree_pk))

    @transition(field=state, source=[State.SERVICES_UP, State.READY], target=State.READY)
    def verify(self, *, urls: dict[str, str] | None = None) -> None:
        """Schedule overlay health checks.

        Pure transition body (BLUEPRINT §4): record any caller-supplied
        URLs, then ``execute_worktree_verify`` enqueued after commit runs
        the overlay's health checks. Source allows re-firing from READY so
        verify can be re-run without bouncing through SERVICES_UP.
        """
        from teatree.core.worktree_tasks import execute_worktree_verify  # noqa: PLC0415

        extra = self._extra()
        if urls:
            extra["urls"] = urls
        self.extra = extra
        worktree_pk = int(self.pk)
        transaction.on_commit(lambda: execute_worktree_verify.enqueue(worktree_pk))

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP, State.READY], target=State.PROVISIONED)
    def db_refresh(self) -> None:
        from django.utils import timezone  # noqa: PLC0415

        extra = self._extra()
        extra["db_refreshed_at"] = timezone.now().isoformat()
        self.extra = extra

    @transition(field=state, source="*", target=State.CREATED)
    def teardown(self) -> None:
        """Schedule docker down + DB drop + git worktree removal.

        Pure transition body (BLUEPRINT §4): snapshot ``db_name`` and
        ``extra`` for the worker, reset them on the row, then enqueue
        ``execute_worktree_teardown`` after commit. The worker runs the
        destructive cleanup (docker compose down, dropdb, ``git worktree
        remove``, branch delete) using the captured snapshot, then
        deletes the Worktree row, so the FSM ``CREATED`` state lasts
        only until the worker fires.
        """
        from teatree.core.worktree_tasks import execute_worktree_teardown  # noqa: PLC0415

        worktree_pk = int(self.pk)
        snapshot_db_name = self.db_name
        snapshot_extra = dict(self.extra or {})
        self.db_name = ""
        self.extra = {}
        transaction.on_commit(lambda: execute_worktree_teardown.enqueue(worktree_pk, snapshot_db_name, snapshot_extra))

    def _build_db_name(self) -> str:
        ticket = cast("Ticket", self.ticket)
        variant_suffix = f"_{ticket.variant}" if ticket.variant else ""
        return f"wt_{ticket.ticket_number}{variant_suffix}"

    def get_extra(self) -> WorktreeExtra:
        return validated_worktree_extra(self.extra)

    def _extra(self) -> WorktreeExtra:
        return validated_worktree_extra(self.extra)


class WorktreeEnvOverride(models.Model):
    """User-declared env var for a worktree's env cache.

    Use ``t3 env set KEY=VALUE`` rather than editing this table directly.
    Keys owned by core (``TICKET_DIR``, ``WT_DB_NAME`` …) are rejected at
    the CLI layer.
    """

    worktree = models.ForeignKey(Worktree, on_delete=models.CASCADE, related_name="env_overrides")
    key = models.CharField(max_length=255)
    value = models.TextField(blank=True)

    class Meta:
        db_table = "teatree_worktree_env_override"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["worktree", "key"], name="uniq_worktree_env_key"),
        ]

    def __str__(self) -> str:
        return f"{self.worktree.pk}:{self.key}"
