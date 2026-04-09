from datetime import timedelta

from django.db import models, transaction
from django.utils import timezone
from django_fsm import FSMField

from teatree.core.managers import TaskManager
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.session import Session
from teatree.core.models.ticket import Ticket


class Task(models.Model):
    class ExecutionTarget(models.TextChoices):
        HEADLESS = "headless", "Headless"
        INTERACTIVE = "interactive", "Interactive"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CLAIMED = "claimed", "Claimed"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="tasks")
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="tasks")
    parent_task = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_tasks",
    )
    phase = models.CharField(max_length=64, blank=True)
    execution_target = models.CharField(
        max_length=32,
        choices=ExecutionTarget.choices,
        default=ExecutionTarget.HEADLESS,
    )
    execution_reason = models.TextField(blank=True)
    status = FSMField(max_length=32, choices=Status.choices, default=Status.PENDING)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_by = models.CharField(max_length=255, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    result_artifact_path = models.CharField(max_length=500, blank=True)

    objects = TaskManager()

    class Meta:
        db_table = "teatree_task"

    def __str__(self) -> str:
        return f"task-{self.pk}-{self.execution_target!s}"

    def claim(self, *, claimed_by: str, lease_seconds: int = 300) -> None:
        now = timezone.now()
        with transaction.atomic():
            locked = Task.objects.select_for_update().get(pk=self.pk)
            if locked.status in {self.Status.COMPLETED, self.Status.FAILED}:
                msg = "Task already finished"
                raise InvalidTransitionError(msg)
            if locked.status == self.Status.CLAIMED and locked.lease_expires_at and locked.lease_expires_at > now:
                msg = "Task already claimed"
                raise InvalidTransitionError(msg)
            locked.status = self.Status.CLAIMED
            locked.claimed_by = claimed_by
            locked.claimed_at = now
            locked.heartbeat_at = now
            locked.lease_expires_at = now + timedelta(seconds=lease_seconds)
            locked.save(
                update_fields=[
                    "status",
                    "claimed_by",
                    "claimed_at",
                    "heartbeat_at",
                    "lease_expires_at",
                ],
            )
        self.refresh_from_db()

    def renew_lease(self, *, lease_seconds: int = 300) -> None:
        now = timezone.now()
        self.heartbeat_at = now
        self.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self.save(update_fields=["heartbeat_at", "lease_expires_at"])

    def route_to_headless(self, *, reason: str = "") -> None:
        self._route(self.ExecutionTarget.HEADLESS, reason)

    def route_to_interactive(self, *, reason: str = "") -> None:
        self._route(self.ExecutionTarget.INTERACTIVE, reason)

    def complete(self, *, result_artifact_path: str = "") -> None:
        self.status = self.Status.COMPLETED
        self.result_artifact_path = result_artifact_path
        self._clear_claim()
        self.save(
            update_fields=[
                "status",
                "result_artifact_path",
                "claimed_at",
                "claimed_by",
                "lease_expires_at",
                "heartbeat_at",
            ],
        )
        self._advance_ticket()

    def _advance_ticket(self) -> None:
        """Auto-advance ticket state based on the completed task's phase."""
        if self._last_attempt_needs_user_input():
            self._schedule_interactive_followup()
            return
        ticket = self.ticket
        ticket.refresh_from_db()
        if self.phase == "reviewing" and ticket.state == Ticket.State.TESTED:
            ticket.review()
            ticket.save()
        elif self.phase == "shipping" and ticket.state == Ticket.State.REVIEWED:
            ticket.ship()
            ticket.save()

    def _last_attempt_needs_user_input(self) -> bool:
        last = self.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
        return bool(last and isinstance(last.result, dict) and last.result.get("needs_user_input"))

    def _schedule_interactive_followup(self) -> "Task":
        """Create a new interactive task for human handoff, carrying the headless session_id."""
        last = self.attempts.order_by("-pk").first()  # ty: ignore[unresolved-attribute]
        reason = str(last.result.get("user_input_reason", "Agent needs human input")) if last else "Agent needs input"
        agent_session_id = last.agent_session_id if last else ""
        session = Session.objects.create(
            ticket=self.ticket,
            agent_id=agent_session_id or "interactive-followup",
        )
        return Task.objects.create(
            ticket=self.ticket,
            session=session,
            phase=self.phase,
            execution_target=self.ExecutionTarget.INTERACTIVE,
            execution_reason=reason,
            parent_task=self,
        )

    def fail(self) -> None:
        self.status = self.Status.FAILED
        self._clear_claim()
        self.save(update_fields=["status", "claimed_at", "claimed_by", "lease_expires_at", "heartbeat_at"])

    def reopen(self) -> None:
        if self.status != self.Status.FAILED:
            msg = f"Can only reopen failed tasks, got '{self.status}'"
            raise InvalidTransitionError(msg)
        self.status = self.Status.PENDING
        self.save(update_fields=["status"])

    def complete_with_attempt(
        self,
        *,
        artifact_path: str = "",
        exit_code: int = 0,
        error: str = "",
        result: dict[str, object] | None = None,
    ) -> "TaskAttempt":
        attempt = TaskAttempt.objects.create(
            task=self,
            execution_target=self.execution_target,
            ended_at=timezone.now(),
            exit_code=exit_code,
            artifact_path=artifact_path,
            error=error,
            result=result or {},
        )
        if exit_code == 0:
            self.complete(result_artifact_path=artifact_path)
        else:
            self.fail()
        return attempt

    def spawn_child_tasks(self, repos: list[str], *, phase: str = "") -> list["Task"]:
        """Create one child task per repo for parallel execution.

        Each child task inherits the ticket and session from the parent.
        The parent can wait for all children by querying ``child_tasks``.
        """
        children = []
        for repo in repos:
            child = Task.objects.create(
                ticket=self.ticket,
                session=self.session,
                phase=phase or self.phase,
                execution_target=self.execution_target,
                execution_reason=f"Repo: {repo}",
                parent_task=self,
            )
            children.append(child)
        return children

    def all_children_done(self) -> bool:
        """Return True if all child tasks have reached a terminal state."""
        children = self.child_tasks.all()  # ty: ignore[unresolved-attribute]
        if not children.exists():
            return True
        return not children.exclude(status__in={self.Status.COMPLETED, self.Status.FAILED}).exists()

    def _route(self, target: ExecutionTarget, reason: str) -> None:
        self.execution_target = target
        self.execution_reason = reason
        self.status = self.Status.PENDING
        self._clear_claim()
        self.save(
            update_fields=[
                "execution_target",
                "execution_reason",
                "status",
                "claimed_at",
                "claimed_by",
                "lease_expires_at",
                "heartbeat_at",
            ],
        )

    def _clear_claim(self) -> None:
        self.claimed_at = None
        self.claimed_by = ""
        self.lease_expires_at = None
        self.heartbeat_at = None


class TaskAttempt(models.Model):
    objects = models.Manager()

    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attempts")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    execution_target = models.CharField(max_length=32, choices=Task.ExecutionTarget.choices)
    error = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    artifact_path = models.CharField(max_length=500, blank=True)
    result = models.JSONField(default=dict, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    cost_usd = models.FloatField(null=True, blank=True)
    num_turns = models.IntegerField(null=True, blank=True)
    launch_url = models.URLField(max_length=500, blank=True)
    agent_session_id = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "teatree_taskattempt"

    def __str__(self) -> str:
        return f"attempt-{self.pk or 'new'!s}"
