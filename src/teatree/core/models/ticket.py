import re
from typing import TYPE_CHECKING, ClassVar, cast

from django.db import models
from django_fsm import FSMField, transition

from teatree.core.managers import TicketManager

if TYPE_CHECKING:
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
        DELIVERED = "delivered", "Delivered"

    STATE_HELPTEXT: ClassVar[dict[str, str]] = {
        "not_started": "Ticket exists but no work has begun",
        "scoped": "Requirements analysed, ready for implementation",
        "started": "Agent is actively working on this ticket",
        "coded": "Implementation complete, awaiting tests",
        "tested": "Tests pass, awaiting code review",
        "reviewed": "Code review approved, ready to ship",
        "shipped": "MRs merged or delivered to staging",
        "in_review": "MR posted, waiting for reviewer feedback",
        "merged": "All MRs merged to default branch",
        "delivered": "Deployed to production or closed",
    }

    overlay = models.CharField(max_length=255)
    issue_url = models.URLField(max_length=500, blank=True)
    variant = models.CharField(max_length=100, blank=True)
    repos = models.JSONField(default=list, blank=True)
    state = FSMField(max_length=32, choices=State.choices, default=State.NOT_STARTED)
    extra = models.JSONField(default=dict, blank=True)

    objects = TicketManager()

    class Meta:
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

    def schedule_shipping(self, *, parent_task: "Task | None" = None) -> "Task":
        """Create a fresh headless shipping task."""
        from teatree.core.models.session import Session  # noqa: PLC0415
        from teatree.core.models.task import Task  # noqa: PLC0415

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
        from teatree.core.models.task import Task  # noqa: PLC0415

        for task in self.tasks.filter(status__in=[Task.Status.PENDING, Task.Status.CLAIMED]):  # type: ignore[attr-defined]  # Django reverse FK
            task.fail()

    def _extra(self) -> "TicketExtra":
        return cast("TicketExtra", self.extra or {})
