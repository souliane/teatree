from pathlib import Path
from typing import ClassVar, cast

from django.db import models
from django_fsm import FSMField, transition

from teatree.core.managers import WorktreeManager
from teatree.core.models.ticket import Ticket
from teatree.core.models.types import WorktreeExtra, validated_worktree_extra
from teatree.utils.postgres_secret import postgres_pass_key


class WorktreeDbNameConflictError(RuntimeError):
    """Raised when another live worktree of a different ticket owns a computed db_name.

    ``db_name`` is keyed on the immutable, unique Ticket pk so a collision
    cannot arise through the normal flow; this guards a hand-built or legacy
    row from clobbering another ticket's database before ``db_import``.
    """


class Worktree(models.Model):
    class State(models.TextChoices):
        CREATED = "created", "Created"
        PROVISIONED = "provisioned", "Provisioned"
        SERVICES_UP = "services_up", "Services up"
        READY = "ready", "Ready"

    overlay = models.CharField(max_length=255)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="worktrees")
    repo_path = models.CharField(
        max_length=500,
        help_text="Repo identifier (e.g. 'org/repo' or a short slug) — NOT a filesystem path. "
        "The on-disk worktree path lives in extra['worktree_path'].",
    )
    branch = models.CharField(max_length=255)
    state = FSMField(max_length=32, choices=State.choices, default=State.CREATED)
    db_name = models.CharField(max_length=255, blank=True)
    extra = models.JSONField(default=dict, blank=True)
    # #2190 Activity-recency signal for the idle-stack reaper. Stamped on
    # ``start_services``/``verify``/``db_refresh`` (the operator-driven
    # lifecycle transitions that prove the stack is in use). A worktree whose
    # ``last_used_at`` is older than ``idle_stack_idle_minutes`` AND has no
    # active session/task is a reap candidate (its containers are stopped and
    # it is demoted to ``provisioned``). Null = never started.
    last_used_at = models.DateTimeField(null=True, blank=True)
    # #2227 E2E-recency signal for the idle-stack reaper. Stamped by
    # ``lifecycle record-e2e-run`` when an E2E/evidence run touches this stack.
    # A worktree whose ``last_e2e_run`` is within ``idle_stack_e2e_recent_minutes``
    # is KEPT by the reaper even when otherwise idle — it is the live target of
    # in-flight evidence work, so reaping it would force a slow re-provision to
    # re-capture. Null = no E2E run has touched it.
    last_e2e_run = models.DateTimeField(null=True, blank=True)

    # Transient (non-DB) carrier for the pre-blank ``(db_name, extra)`` snapshot
    # the ``teardown`` body captures, read by the post_transition receiver that
    # enqueues ``execute_worktree_teardown`` (#2385). Default empty so a row
    # that never tore down still reads safely.
    teardown_snapshot: "tuple[str, WorktreeExtra]" = ("", WorktreeExtra())

    objects = WorktreeManager()

    class Meta:
        db_table = "teatree_worktree"

    def __str__(self) -> str:
        return str(self.repo_path)

    @property
    def worktree_path(self) -> str:
        """On-disk path to the materialised git worktree, or '' before provisioning."""
        extra = self.extra if isinstance(self.extra, dict) else {}
        return str(extra.get("worktree_path", ""))

    @property
    def is_stale(self) -> bool:
        """True if this row claims a worktree path that no longer exists on disk."""
        path = self.worktree_path
        return bool(path) and not Path(path).exists()

    @property
    def pass_key(self) -> str:
        """Canonical, collision-free ``pass`` key for this worktree's postgres password.

        Keyed on the immutable, unique Ticket pk (NOT the derived, non-unique
        ``ticket_number``), so two tickets sharing a trailing issue number never
        share one secret entry. Ticket-scoped — the same canonical key the
        db_name uses — so a ticket's sibling repos share one database password.
        """
        return postgres_pass_key(self.ticket_id)  # ty: ignore[unresolved-attribute]  # Django FK accessor

    @transition(field=state, source=[State.CREATED, State.PROVISIONED], target=State.PROVISIONED)
    def provision(self) -> None:
        """Schedule heavy provisioning side-effects.

        Pure transition body (BLUEPRINT §4): state + ``db_name`` here, then
        ``execute_worktree_provision`` enqueued after commit so the env
        cache, direnv + prek setup, DB import, overlay steps and health
        checks all run in a worker. Source ``[CREATED, PROVISIONED]`` makes
        re-firing idempotent — a previous worker that crashed mid-import
        can be retried without going back to CREATED.

        The ``execute_worktree_provision`` enqueue is the ``post_transition``
        receiver's job (``teatree.core.signals``), keyed on the transition
        name — the body stays free of the worktree-tasks up-edge (#2385).
        """
        self.db_name = self._build_db_name()

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP, State.READY], target=State.SERVICES_UP)
    def start_services(self, *, services: list[str] | None = None) -> None:
        """Schedule docker compose up.

        Pure transition body (BLUEPRINT §4): record the intended services,
        then ``execute_worktree_start`` enqueued after commit drives the
        actual ``docker compose up``. Source allows re-firing from
        SERVICES_UP / READY so a partially-failed boot can be retried.

        The ``execute_worktree_start`` enqueue is the ``post_transition``
        receiver's job (``teatree.core.signals``), keyed on the transition
        name (#2385).
        """
        from django.utils import timezone  # noqa: PLC0415

        if services is not None:
            extra = self._extra()
            extra["services"] = services
            self.extra = extra
        self.last_used_at = timezone.now()

    @transition(field=state, source=[State.SERVICES_UP, State.READY], target=State.READY)
    def verify(self, *, urls: dict[str, str] | None = None) -> None:
        """Schedule overlay health checks.

        Pure transition body (BLUEPRINT §4): record any caller-supplied
        URLs, then ``execute_worktree_verify`` enqueued after commit runs
        the overlay's health checks. Source allows re-firing from READY so
        verify can be re-run without bouncing through SERVICES_UP.

        The ``execute_worktree_verify`` enqueue is the ``post_transition``
        receiver's job (``teatree.core.signals``), keyed on the transition
        name (#2385).
        """
        from django.utils import timezone  # noqa: PLC0415

        extra = self._extra()
        if urls:
            extra["urls"] = urls
        self.extra = extra
        self.last_used_at = timezone.now()

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP, State.READY], target=State.PROVISIONED)
    def db_refresh(self) -> None:
        from django.utils import timezone  # noqa: PLC0415

        extra = self._extra()
        extra["db_refreshed_at"] = timezone.now().isoformat()
        self.extra = extra
        self.last_used_at = timezone.now()

    @transition(field=state, source=[State.SERVICES_UP, State.READY], target=State.PROVISIONED)
    def stop_services(self) -> None:
        """Schedule a reversible docker-compose-down → demote to ``provisioned``.

        Distinct from ``teardown`` (which destroys the DB + git worktree) and
        from ``db_refresh`` (which re-imports the DB). ``stop_services`` only
        brings the whole compose project DOWN — the DB, the git worktree, and
        ``extra`` are all preserved, so a later ``start_services`` is a fast
        resume, not a re-provision. The idle-stack reaper uses this to free the
        host's RAM + a ``max_concurrent_local_stacks`` slot for an idle stack
        without any data-loss risk.

        Pure transition body (BLUEPRINT §4): the FSM advances to PROVISIONED
        here, then ``execute_worktree_stop`` enqueued after commit drives the
        actual ``docker compose down``. Source ``[SERVICES_UP, READY]`` — a
        worktree must be running to be stopped.

        The ``execute_worktree_stop`` enqueue is the ``post_transition``
        receiver's job (``teatree.core.signals``), keyed on the transition
        name (#2385).
        """

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

        The body BLANKS ``db_name`` / ``extra`` here, so the
        ``post_transition`` receiver (``teatree.core.signals``) cannot read
        them off the live row — it would enqueue a teardown with an empty
        DB name and never drop the database. The PRE-BLANK values are
        stashed on the transient :attr:`teardown_snapshot` attribute, which
        the receiver reads to enqueue ``execute_worktree_teardown`` with the
        correct snapshot (#2385).
        """
        self.teardown_snapshot = (self.db_name, self._extra())
        self.db_name = ""
        self.extra = {}

    def _build_db_name(self) -> str:
        ticket = cast("Ticket", self.ticket)
        variant_suffix = f"_{ticket.variant}" if ticket.variant else ""
        # Keyed on the immutable, unique Ticket pk (not the derived, non-unique
        # ``ticket_number``): two tickets sharing a trailing issue number must
        # never resolve to one database. Ticket-scoped (not worktree-scoped) so a
        # ticket's sibling repos share one database, as the per-ticket env cache
        # requires.
        return f"wt_{ticket.pk}{variant_suffix}"

    def assert_db_name_unclaimed(self) -> None:
        """Fail loud if another LIVE worktree of a DIFFERENT ticket owns ``db_name``.

        Defense-in-depth guard the provision runner calls before ``db_import``:
        ``db_name`` is ticket-pk-keyed so a collision cannot arise through the
        normal flow, but a hand-built or legacy row could still clobber another
        ticket's database. CREATED rows own no database yet and are excluded.
        """
        if not self.db_name:
            return
        ticket_pk = self.ticket_id  # ty: ignore[unresolved-attribute]  # Django FK accessor
        conflict = (
            Worktree.objects.exclude(pk=self.pk)
            .exclude(ticket_id=ticket_pk)
            .exclude(state=Worktree.State.CREATED)
            .filter(db_name=self.db_name)
            .first()
        )
        if conflict is not None:
            msg = (
                f"db_name {self.db_name!r} is owned by another live worktree "
                f"(#{conflict.pk}); refusing db_import for worktree #{self.pk} to avoid clobber."
            )
            raise WorktreeDbNameConflictError(msg)

    def get_extra(self) -> WorktreeExtra:
        return validated_worktree_extra(self.extra)

    def _extra(self) -> WorktreeExtra:
        return validated_worktree_extra(self.extra)


class WorktreeEnvOverride(models.Model):
    """User-declared env var for a worktree's env cache.

    Use ``t3 teatree env set-var KEY=VALUE`` rather than editing this table directly.
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
