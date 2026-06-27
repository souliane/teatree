from typing import TYPE_CHECKING, cast

from django.db import models

from teatree.core.modelkit.gate_registry import get
from teatree.core.models.task import Task
from teatree.core.repair_loop import terminal_reason_fingerprint

if TYPE_CHECKING:
    from teatree.core.cost import AttemptUsage, CostBreakdown


class TaskAttemptQuerySet(models.QuerySet):
    def headless(self) -> "TaskAttemptQuerySet":
        """Only the attempts that ran a billed detached headless-SDK run.

        SDK-equivalent billing covers headless usage only — interactive turns
        run inside the user's own session, not against the credit.
        """
        return self.filter(execution_target=Task.ExecutionTarget.HEADLESS)

    def usages(self) -> "list[AttemptUsage]":
        """Map each attempt to the :class:`AttemptUsage` the cost layer reads."""
        AttemptUsage = cast("type[AttemptUsage]", get("cost", "AttemptUsage"))  # noqa: N806

        return [
            AttemptUsage(
                model=row.model or None,
                reported_cost_usd=row.cost_usd,
                input_tokens=row.input_tokens or 0,
                output_tokens=row.output_tokens or 0,
                cache_read_tokens=row.cache_read_tokens or 0,
                cache_write_tokens=row.cache_write_tokens or 0,
            )
            for row in self.only(
                "model",
                "cost_usd",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        ]

    def cost_breakdown(self) -> "CostBreakdown":
        """SDK-equivalent spend across the attempts in this queryset."""
        CostBreakdown = cast("type[CostBreakdown]", get("cost", "CostBreakdown"))  # noqa: N806

        return CostBreakdown.from_usages(self.usages())


class TaskAttempt(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attempts")
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    execution_target = models.CharField(max_length=32, choices=Task.ExecutionTarget.choices)
    error = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    artifact_path = models.CharField(max_length=500, blank=True)
    result = models.JSONField(default=dict, blank=True)
    model = models.CharField(max_length=128, blank=True)
    input_tokens = models.IntegerField(null=True, blank=True)
    output_tokens = models.IntegerField(null=True, blank=True)
    cache_read_tokens = models.IntegerField(null=True, blank=True)
    cache_write_tokens = models.IntegerField(null=True, blank=True)
    cost_usd = models.FloatField(null=True, blank=True)
    num_turns = models.IntegerField(null=True, blank=True)
    launch_url = models.URLField(max_length=500, blank=True)
    agent_session_id = models.CharField(max_length=255, blank=True)
    # #2009 repair-loop budget: 1-based attempt number for this attempt's
    # (ticket, normalized-phase), spanning re-queued Task rows. Auto-stamped on
    # insert; 0 only on a transient unsaved instance.
    iteration = models.PositiveIntegerField(default=0)
    # #2009 stall detection: stable hash of this attempt's terminal reason
    # (its ``error``), normalized so transient noise does not defeat the
    # identical-failure check. Empty for a clean (non-failing) attempt.
    error_fingerprint = models.CharField(max_length=64, blank=True, default="")

    objects = TaskAttemptQuerySet.as_manager()

    class Meta:
        db_table = "teatree_taskattempt"

    def __str__(self) -> str:
        return f"attempt-{self.pk or 'new'!s}"

    def save(self, *args: object, **kwargs: object) -> None:
        if self._state.adding:
            self._stamp_repair_loop_fields()
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def _stamp_repair_loop_fields(self) -> None:
        """Stamp the iteration counter + error fingerprint on insert (#2009).

        The single chokepoint every attempt-creation site funnels through, so the
        budget fields cannot drift between the headless recorder, the in-session
        recorder, and the operator out-of-band paths. Each is stamped only when
        unset, so an explicit value (a backfill or a test) is never clobbered.
        """
        if not self.error_fingerprint:
            self.error_fingerprint = terminal_reason_fingerprint(self.error)
        if not self.iteration and self.task_id:  # ty: ignore[unresolved-attribute]
            self.iteration = self.task.phase_iteration_count() + 1
