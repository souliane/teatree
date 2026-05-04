import os
import re
from typing import TYPE_CHECKING, ClassVar, cast

from django.db import models, transaction
from django_fsm import FSMField, TransitionNotAllowed, transition

from teatree.core.managers import TicketManager
from teatree.utils import redis_container


def _auto_ship_enabled() -> bool:
    """Return True when ``T3_AUTO_SHIP`` opts into headless shipping.

    Default is ``False`` — shipping tasks land in the interactive queue so the
    user must approve the push explicitly. Set ``T3_AUTO_SHIP=true`` in
    ``~/.teatree`` to allow headless shipping.
    """
    return os.environ.get("T3_AUTO_SHIP", "").lower() == "true"


if TYPE_CHECKING:
    from teatree.core.models.session import Session
    from teatree.core.models.task import Task
    from teatree.core.models.types import TicketExtra


class Ticket(models.Model):
    class State(models.TextChoices):
        NOT_STARTED = "not_started", "Not started"
        SCOPED = "scoped", "Scoped"
        STARTED = "started", "Started"
        CODED = "coded", "Coded"
        TESTED = "tested", "Tested"
        REVIEWED = "reviewed", "Reviewed"
        SHIPPED = "shipped", "Shipped"
        IN_REVIEW = "in_review", "In review"
        MERGED = "merged", "Merged"
        RETROSPECTED = "retrospected", "Retrospected"
        DELIVERED = "delivered", "Delivered"
        IGNORED = "ignored", "Ignored"

    overlay = models.CharField(max_length=255)
    issue_url = models.URLField(max_length=500, blank=True)
    variant = models.CharField(max_length=100, blank=True)
    repos = models.JSONField(default=list, blank=True)
    state = FSMField(max_length=32, choices=State.choices, default=State.NOT_STARTED)
    extra = models.JSONField(default=dict, blank=True)
    redis_db_index = models.IntegerField(null=True, blank=True, unique=True)

    objects = TicketManager()

    class Meta:
        db_table = "teatree_ticket"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["issue_url"],
                name="unique_nonempty_issue_url",
                condition=~models.Q(issue_url=""),
            ),
        ]

    def __str__(self) -> str:
        return str(self.issue_url or f"ticket-{self.pk}")

    @property
    def ticket_number(self) -> str:
        match = re.search(r"(\d+)$", self.issue_url)
        return match.group(1) if match else str(self.pk)

    @transition(field=state, source=State.NOT_STARTED, target=State.SCOPED)
    def scope(
        self,
        *,
        issue_url: str | None = None,
        variant: str | None = None,
        repos: list[str] | None = None,
    ) -> None:
        if issue_url is not None:
            self.issue_url = issue_url
        if variant is not None:
            self.variant = variant
        if repos is not None:
            self.repos = repos

    @transition(field=state, source=[State.SCOPED, State.STARTED], target=State.STARTED)
    def start(self) -> None:
        """Schedule worktree provisioning + coding task.

        The worker creates per-repo git worktrees, then calls
        ``schedule_coding()`` once the layout exists. FSM invariant (BLUEPRINT
        §4): transition bodies stay pure — long I/O is offloaded to an
        ``@task`` worker, enqueued after commit so the state change and the
        queued work land atomically.

        Source ``[SCOPED, STARTED]`` makes re-firing idempotent: if the previous
        provisioning worker failed, the operator can re-call ``start()``
        without rolling back through ``rework``. The worker's own state guard
        prevents duplicate work when provisioning already succeeded.
        """
        from teatree.core.tasks import execute_provision  # noqa: PLC0415

        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_provision.enqueue(ticket_pk))

    @transition(field=state, source=State.STARTED, target=State.CODED)
    def code(self) -> None:
        self._consume_pending_phase_tasks("coding")
        self.schedule_testing()

    @transition(field=state, source=State.CODED, target=State.TESTED)
    def test(self, *, passed: bool = True) -> None:
        extra = self._extra()
        extra["tests_passed"] = passed
        self.extra = extra
        self._consume_pending_phase_tasks("testing")
        self.schedule_review()

    @transition(
        field=state,
        source=State.TESTED,
        target=State.REVIEWED,
        conditions=[lambda t: t.tasks.filter(phase="reviewing", status="completed").exists()],
    )
    def review(self) -> None:
        self._consume_pending_phase_tasks("reviewing")
        self.schedule_shipping()

    def schedule_coding(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless coding task after scoping completes."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        session = Session.objects.create(ticket=self, agent_id="coding")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled coding — implement the ticket",
            parent_task=parent_task,
        )

    def schedule_testing(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless testing task after coding completes."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        session = Session.objects.create(ticket=self, agent_id="testing")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="testing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled testing — run + QA the coding work",
            parent_task=parent_task,
        )

    def schedule_review(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless review+retro task (new session for bias-free evaluation)."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        session = Session.objects.create(ticket=self, agent_id="review")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled review + retro — fresh agent, no bias",
            parent_task=parent_task,
        )

    def schedule_review_in_session(self, session: "Session", *, parent_task: "Task | None" = None) -> "Task":
        """Create a review task within an existing session (sub-agent, not a new session)."""
        from teatree.core.models.task import Task  # noqa: PLC0415

        return Task.objects.create(
            ticket=self,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-review before shipping — sub-agent in current session",
            parent_task=parent_task,
        )

    def schedule_shipping(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a shipping task. Defaults to interactive; headless when ``T3_AUTO_SHIP=true``."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

        session = Session.objects.create(ticket=self, agent_id="shipping")
        if _auto_ship_enabled():
            target = Task.ExecutionTarget.HEADLESS
            reason = "Auto-scheduled shipping — T3_AUTO_SHIP=true, push will proceed headlessly"
        else:
            target = Task.ExecutionTarget.INTERACTIVE
            reason = "Auto-scheduled shipping — gated for user approval (set T3_AUTO_SHIP=true to skip)"
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="shipping",
            execution_target=target,
            execution_reason=reason,
            parent_task=parent_task,
        )

    @transition(field=state, source=[State.REVIEWED, State.SHIPPED], target=State.SHIPPED)
    def ship(self) -> None:
        """Schedule push + MR creation.

        The worker pushes the worktree branch, opens the merge request, and
        calls ``request_review()`` on success. FSM invariant (BLUEPRINT §4):
        transition bodies stay pure — long I/O is offloaded to an ``@task``
        worker, enqueued after commit so the state change and the queued work
        land atomically.

        Source ``[REVIEWED, SHIPPED]`` makes re-firing idempotent: if the
        previous ship worker failed (push rejected, code host unavailable,
        credentials missing), the operator can re-call ``ship()`` to retry.
        The worker's own state guard skips duplicate work if push already
        succeeded.
        """
        from teatree.core.tasks import execute_ship  # noqa: PLC0415

        self._consume_pending_phase_tasks("shipping")
        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_ship.enqueue(ticket_pk))

    @transition(field=state, source=State.SHIPPED, target=State.IN_REVIEW)
    def request_review(self) -> None:
        pass

    @transition(field=state, source=[State.IN_REVIEW, State.MERGED], target=State.MERGED)
    def mark_merged(self) -> None:
        """Schedule worktree teardown.

        The worker removes git worktrees, deletes the local branch, drops the
        per-worktree DB and runs overlay cleanup hooks. FSM invariant
        (BLUEPRINT §4): transition bodies stay pure — long I/O is offloaded
        to an ``@task`` worker, enqueued after commit so the state change and
        the queued work land atomically.

        Source ``[IN_REVIEW, MERGED]`` makes re-firing idempotent: if a
        previous teardown reported errors, the operator can re-call
        ``mark_merged()`` to retry. The worker is best-effort and does not
        advance the FSM, so retries are safe.
        """
        from teatree.core.tasks import execute_teardown  # noqa: PLC0415

        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_teardown.enqueue(ticket_pk))

    @transition(field=state, source=[State.MERGED, State.RETROSPECTED], target=State.RETROSPECTED)
    def retrospect(self) -> None:
        """Schedule retrospection I/O.

        The worker writes retro artifacts and calls ``mark_delivered()`` on
        success. FSM invariant (BLUEPRINT §4): transition bodies stay pure —
        long I/O is offloaded to an ``@task`` worker, enqueued after commit so
        the state change and the queued work land atomically.

        Source ``[MERGED, RETROSPECTED]`` makes re-firing idempotent: if a
        previous retro worker failed, the operator can re-call ``retrospect()``
        to retry. The worker's own state guard skips when retrospection
        already produced its artifacts.
        """
        from teatree.core.tasks import execute_retrospect  # noqa: PLC0415

        ticket_pk = int(self.pk)
        transaction.on_commit(lambda: execute_retrospect.enqueue(ticket_pk))

    @transition(field=state, source=State.RETROSPECTED, target=State.DELIVERED)
    def mark_delivered(self) -> None:
        pass

    @transition(field=state, source=[State.CODED, State.TESTED, State.REVIEWED], target=State.STARTED)
    def rework(self) -> None:
        extra = self._extra()
        extra.pop("tests_passed", None)
        self.extra = extra
        self._cancel_pending_tasks()

    @transition(
        field=state,
        source=[
            State.NOT_STARTED,
            State.SCOPED,
            State.STARTED,
            State.CODED,
            State.TESTED,
            State.REVIEWED,
            State.SHIPPED,
            State.IN_REVIEW,
            State.MERGED,
            State.RETROSPECTED,
        ],
        target=State.IGNORED,
    )
    def ignore(self) -> None:
        extra = self._extra()
        extra["ignored_from"] = self.state
        self.extra = extra

    def unignore(self) -> None:
        if self.state != self.State.IGNORED:
            msg = f"Can't unignore from state '{self.state}'"
            raise TransitionNotAllowed(msg)
        extra = self._extra()
        previous = extra.pop("ignored_from", self.State.NOT_STARTED)
        self.extra = extra
        self.state = str(previous)

    def release_redis_slot(self) -> None:
        """FLUSHDB on the ticket's Redis DB index and clear the field."""
        if self.redis_db_index is None:
            return
        index = self.redis_db_index
        redis_container.flushdb(index)
        self.redis_db_index = None
        self.save(update_fields=["redis_db_index"])

    def _cancel_pending_tasks(self) -> None:
        """Fail all pending/claimed tasks when reworking."""
        from teatree.core.models.task import Task  # noqa: PLC0415

        for task in self.tasks.filter(status__in=[Task.Status.PENDING, Task.Status.CLAIMED]):  # type: ignore[attr-defined]  # Django reverse FK
            task.fail()

    def _consume_pending_phase_tasks(self, phase: str) -> None:
        """Mark non-terminal tasks for ``phase`` as COMPLETED.

        FSM transitions advance ticket state via two paths: the task-driven
        chain (``Task.complete()`` → ``_advance_ticket()`` → transition body),
        and direct CLI/API calls (e.g. ``pr.py`` calling ``ticket.ship()``).
        On the task-driven path the task is already COMPLETED before this runs
        — the filter is empty and this is a no-op. On the direct path the
        previously-scheduled phase task is orphaned in PENDING/CLAIMED and
        would be picked up later as a zombie session; consume it now.
        """
        from teatree.core.models.task import Task  # noqa: PLC0415

        self.tasks.filter(  # type: ignore[attr-defined]  # Django reverse FK
            phase=phase,
            status__in=[Task.Status.PENDING, Task.Status.CLAIMED],
        ).update(
            status=Task.Status.COMPLETED,
            claimed_at=None,
            claimed_by="",
            lease_expires_at=None,
            heartbeat_at=None,
        )

    def _extra(self) -> "TicketExtra":
        return cast("TicketExtra", self.extra or {})
