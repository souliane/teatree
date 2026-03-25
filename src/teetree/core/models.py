import re
import socket
from datetime import timedelta
from pathlib import Path
from typing import ClassVar, cast

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django_fsm import FSMField, transition

from teetree.config import load_config
from teetree.core.managers import SessionManager, TaskManager, TicketManager, WorktreeManager
from teetree.utils import ports as port_utils

type Ports = dict[str, int]


def _workspace_dir() -> Path:
    configured = getattr(settings, "T3_WORKSPACE_DIR", "")
    if configured:
        return Path(str(configured)).expanduser()
    return load_config().workspace_dir


class InvalidTransitionError(ValueError):
    pass


class QualityGateError(ValueError):
    pass


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
        DELIVERED = "delivered", "Delivered"

    issue_url = models.URLField(max_length=500, blank=True)
    variant = models.CharField(max_length=100, blank=True)
    repos = models.JSONField(default=list, blank=True)
    state = FSMField(max_length=32, choices=State.choices, default=State.NOT_STARTED)
    extra = models.JSONField(default=dict, blank=True)

    objects = TicketManager()

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

    @transition(field=state, source=State.SCOPED, target=State.STARTED)
    def start(self) -> None:
        pass

    @transition(field=state, source=State.STARTED, target=State.CODED)
    def code(self) -> None:
        pass

    @transition(field=state, source=State.CODED, target=State.TESTED)
    def test(self, *, passed: bool = True) -> None:
        extra = self._extra()
        extra["tests_passed"] = passed
        self.extra = extra
        self.schedule_review()

    @transition(
        field=state,
        source=State.TESTED,
        target=State.REVIEWED,
        conditions=[lambda t: t.tasks.filter(phase="reviewing", status="completed").exists()],
    )
    def review(self) -> None:
        self.schedule_shipping()

    def schedule_review(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless review+retro task (new session for bias-free evaluation)."""
        session = Session.objects.create(ticket=self, agent_id="review")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled review + retro — fresh agent, no bias",
            parent_task=parent_task,
        )

    def schedule_shipping(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless shipping task."""
        session = Session.objects.create(ticket=self, agent_id="shipping")
        return Task.objects.create(
            ticket=self,
            session=session,
            phase="shipping",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="Auto-scheduled shipping — MR creation and delivery",
            parent_task=parent_task,
        )

    @transition(field=state, source=State.REVIEWED, target=State.SHIPPED)
    def ship(self, *, mr_urls: list[str] | None = None) -> None:
        extra = self._extra()
        extra["mr_urls"] = mr_urls or []
        self.extra = extra

    @transition(field=state, source=State.SHIPPED, target=State.IN_REVIEW)
    def request_review(self) -> None:
        pass

    @transition(field=state, source=State.IN_REVIEW, target=State.MERGED)
    def mark_merged(self) -> None:
        pass

    @transition(field=state, source=State.MERGED, target=State.DELIVERED)
    def mark_delivered(self) -> None:
        pass

    @transition(field=state, source=[State.CODED, State.TESTED, State.REVIEWED], target=State.STARTED)
    def rework(self) -> None:
        extra = self._extra()
        extra.pop("tests_passed", None)
        self.extra = extra
        self._cancel_pending_tasks()

    def _cancel_pending_tasks(self) -> None:
        """Fail all pending/claimed tasks when reworking."""
        for task in self.tasks.filter(status__in=[Task.Status.PENDING, Task.Status.CLAIMED]):  # ty: ignore[unresolved-attribute]
            task.fail()

    def _extra(self) -> dict[str, object]:
        return cast("dict[str, object]", self.extra or {})


class Worktree(models.Model):
    class State(models.TextChoices):
        CREATED = "created", "Created"
        PROVISIONED = "provisioned", "Provisioned"
        SERVICES_UP = "services_up", "Services up"
        READY = "ready", "Ready"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="worktrees")
    repo_path = models.CharField(max_length=500)
    branch = models.CharField(max_length=255)
    state = FSMField(max_length=32, choices=State.choices, default=State.CREATED)
    ports = models.JSONField(default=dict, blank=True)
    db_name = models.CharField(max_length=255, blank=True)
    extra = models.JSONField(default=dict, blank=True)

    objects = WorktreeManager()

    def __str__(self) -> str:
        return str(self.repo_path)

    @transition(field=state, source=State.CREATED, target=State.PROVISIONED)
    def provision(self, *, ports: Ports | None = None) -> None:
        self.ports = ports or self._allocate_ports()
        self.db_name = self._build_db_name()

    @transition(field=state, source=State.PROVISIONED, target=State.SERVICES_UP)
    def start_services(self, *, services: list[str] | None = None) -> None:
        if services is not None:
            extra = self._extra()
            extra["services"] = services
            self.extra = extra

    @transition(field=state, source=State.SERVICES_UP, target=State.READY)
    def verify(self) -> None:
        extra = self._extra()
        ports = self._ports()
        extra["urls"] = {
            name: f"http://localhost:{port}" for name, port in ports.items() if name not in {"postgres", "redis"}
        }
        self.extra = extra

    @transition(field=state, source=[State.PROVISIONED, State.SERVICES_UP, State.READY], target=State.PROVISIONED)
    def db_refresh(self) -> None:
        extra = self._extra()
        extra["db_refreshed_at"] = timezone.now().isoformat()
        self.extra = extra

    @transition(field=state, source="*", target=State.CREATED)
    def teardown(self) -> None:
        self.ports = {}
        self.db_name = ""
        self.extra = {}

    def _allocate_ports(self) -> Ports:
        reserved_ports: port_utils.ReservedPorts = {
            "backend": set(),
            "frontend": set(),
            "postgres": set(),
        }
        for ports in Worktree.objects.exclude(pk=self.pk).values_list("ports", flat=True):
            if not isinstance(ports, dict):
                continue
            for name in reserved_ports:
                value = ports.get(name)
                if isinstance(value, int):
                    reserved_ports[name].add(value)
        backend, frontend, postgres, redis = port_utils.find_free_ports(
            str(_workspace_dir()),
            share_db_server=False,
            reserved_ports=reserved_ports,
        )
        return {
            "backend": backend,
            "frontend": frontend,
            "postgres": postgres,
            "redis": redis,
        }

    def _build_db_name(self) -> str:
        ticket = cast("Ticket", self.ticket)
        variant_suffix = f"_{ticket.variant}" if ticket.variant else ""
        return f"wt_{ticket.ticket_number}{variant_suffix}"

    @staticmethod
    def _port_available(port: int) -> bool:
        """Check if a port is available by attempting to bind to it."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    def refresh_ports_if_needed(self) -> bool:
        """Allocate ports if missing, but never reallocate already-assigned ports.

        A port being "in use" is *expected* when the worktree's service is
        running.  Only allocate when the port dict is incomplete (missing
        required keys).
        """
        current = self._ports()
        required = ("backend", "frontend", "postgres")
        if current and all(name in current for name in required):
            return False

        new_ports = self._allocate_ports()
        # Preserve any already-assigned ports (don't reallocate running services)
        merged = {**new_ports, **{k: v for k, v in current.items() if v}}
        if current == merged:
            return False

        self.ports = merged
        self.save(update_fields=["ports"])
        return True

    def _extra(self) -> dict[str, object]:
        return cast("dict[str, object]", self.extra or {})

    def _ports(self) -> Ports:
        return cast("Ports", self.ports or {})


class Session(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="sessions")
    visited_phases = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    agent_id = models.CharField(max_length=255, blank=True)

    objects = SessionManager()

    _REQUIRED_PHASES: ClassVar[dict[str, list[str]]] = {
        "reviewing": ["testing"],
        "shipping": ["testing", "reviewing"],
        "requesting_review": ["shipping"],
    }

    def __str__(self) -> str:
        return str(self.agent_id or f"session-{self.pk}")

    def visit_phase(self, phase: str) -> None:
        visited_phases = self._visited_phases()
        if phase not in visited_phases:
            self.visited_phases = [*visited_phases, phase]
            self.save(update_fields=["visited_phases"])

    def has_visited(self, phase: str) -> bool:
        return phase in self._visited_phases()

    def check_gate(self, target_phase: str, *, force: bool = False) -> None:
        if force:
            return

        visited_phases = self._visited_phases()
        missing = [phase for phase in self._REQUIRED_PHASES.get(target_phase, []) if phase not in visited_phases]
        if missing:
            joined = ", ".join(missing)
            msg = f"{target_phase} requires: {joined}"
            raise QualityGateError(msg)

    def begin_manual_handoff(self) -> None:
        self.ended_at = timezone.now()
        self.save(update_fields=["ended_at"])

    def _visited_phases(self) -> list[str]:
        return cast("list[str]", self.visited_phases or [])


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
        self.status = locked.status
        self.claimed_by = locked.claimed_by
        self.claimed_at = locked.claimed_at
        self.heartbeat_at = locked.heartbeat_at
        self.lease_expires_at = locked.lease_expires_at

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
        # The ticket.review() / ticket.ship() transitions call schedule_shipping()
        # etc. via Ticket methods — parent_task linkage for those is set below
        # in the Ticket.test() and Ticket.review() callers when they have context.

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
    launch_url = models.URLField(max_length=500, blank=True)
    agent_session_id = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        return f"attempt-{self.pk or 'new'!s}"
