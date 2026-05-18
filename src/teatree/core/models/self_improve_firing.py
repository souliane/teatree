"""Self-improve monitor durable firing record (#979).

One row per ``(detector, dedup_key)`` pair documenting that a self-improve
detector observed a smell.  Re-fires update ``last_fired_at`` /
``last_action`` / ``action_count`` in place rather than creating new rows
— the unique constraint and ``state_hash`` together implement the
dedup + cool-down semantics described in BLUEPRINT § 5.7.

The model is intentionally minimal: it stores the receipt of a firing,
not the work that motivated it.  Detectors carry their evidence in the
``payload`` JSON column so the post-hoc auditor (``t3 loop self-improve
status``) can reconstruct what the detector saw without re-running it.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class SelfImproveFiring(models.Model):
    """One detector observation, deduped on ``(detector, dedup_key)``."""

    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARN = "warn", "Warn"
        ERROR = "error", "Error"

    class Action(models.TextChoices):
        LOG = "log", "Log"
        STATUSLINE = "statusline", "Statusline"
        SLACK = "slack", "Slack"
        TICKET = "ticket", "Ticket"
        AUTO_FIX = "auto_fix", "Auto-fix"

    detector = models.CharField(max_length=128)
    dedup_key = models.CharField(max_length=255)
    state_hash = models.CharField(max_length=64)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    first_fired_at = models.DateTimeField(default=timezone.now)
    last_fired_at = models.DateTimeField(default=timezone.now)
    last_action = models.CharField(max_length=16, choices=Action.choices, default=Action.LOG)
    action_count = models.IntegerField(default=1)
    ticket = models.ForeignKey(
        "core.Ticket",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="self_improve_firings",
    )
    payload = models.JSONField(default=dict, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_self_improve_firing"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["detector", "dedup_key"],
                name="unique_self_improve_detector_dedup_key",
            ),
        ]
        indexes: ClassVar = [
            models.Index(fields=["detector", "last_fired_at"], name="si_firing_det_last_idx"),
        ]
        ordering: ClassVar = ["-last_fired_at"]

    def __str__(self) -> str:
        return f"self-improve<{self.detector}:{self.dedup_key}>"
