from typing import ClassVar

from django.db import models
from django.utils import timezone
from django_fsm import FSMField, transition


class PullRequest(models.Model):
    class State(models.TextChoices):
        OPEN = "open", "Open"
        REVIEW_REQUESTED = "review_requested", "Review requested"
        APPROVED = "approved", "Approved"
        MERGED = "merged", "Merged"

    ticket = models.ForeignKey("core.Ticket", on_delete=models.CASCADE, related_name="pull_requests")
    overlay = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=500)
    repo = models.CharField(max_length=255)
    iid = models.CharField(max_length=50)
    slack_url = models.URLField(max_length=500, blank=True)
    review_requested_at = models.DateTimeField(null=True, blank=True)
    state = FSMField(max_length=32, choices=State.choices, default=State.OPEN)

    class Meta:
        db_table = "teatree_pull_request"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["url"],
                name="unique_pull_request_url",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.repo} #{self.iid}"

    @transition(field=state, source=State.OPEN, target=State.REVIEW_REQUESTED)
    def request_review(self, *, slack_url: str = "", review_requested_at: "models.DateTimeField | None" = None) -> None:
        if slack_url:
            self.slack_url = slack_url
        self.review_requested_at = review_requested_at or timezone.now()

    @transition(field=state, source=State.REVIEW_REQUESTED, target=State.APPROVED)
    def approve(self) -> None:
        pass

    @transition(field=state, source=[State.OPEN, State.REVIEW_REQUESTED, State.APPROVED], target=State.MERGED)
    def mark_merged(self) -> None:
        pass
