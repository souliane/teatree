"""Durable correction of a merge whose content never reached the default branch."""

from dataclasses import dataclass
from typing import ClassVar

from django.db import models
from django.utils import timezone


@dataclass(frozen=True, slots=True)
class ManualVoidPermit:
    presence: str

    def require_absent(self) -> None:
        if self.presence != "absent":
            msg = f"Merge void refused: content is {self.presence}"
            raise ValueError(msg)


class MergeVoid(models.Model):
    ticket = models.ForeignKey("core.Ticket", on_delete=models.PROTECT, related_name="merge_voids")
    pull_request = models.OneToOneField("core.PullRequest", on_delete=models.PROTECT, related_name="merge_void")
    actor = models.CharField(max_length=255)
    reason = models.TextField()
    voided_at = models.DateTimeField(default=timezone.now)
    ticket_state_before_merge = models.CharField(max_length=32)
    pr_state_before_merge = models.CharField(max_length=32)
    head_sha = models.CharField(max_length=64)
    evidence = models.JSONField()

    class Meta:
        db_table = "teatree_merge_void"
        ordering: ClassVar = ["-voided_at"]

    def __str__(self) -> str:
        return f"merge-void<{self.pull_request.repo}#{self.pull_request.iid}>"

    def summary(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "reason": self.reason,
            "voided_at": self.voided_at.isoformat(),
            "head_sha": self.head_sha,
            "evidence": self.evidence,
            "ticket_state_before_merge": self.ticket_state_before_merge,
            "pr_state_before_merge": self.pr_state_before_merge,
            "pull_request_id": self.pull_request_id,
        }
