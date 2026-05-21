"""Idempotency ledger for failed-E2E Slack-post scans (#1295 capability E).

When the loop's ``FailedE2EPostsScanner`` parses a failed-E2E Slack post,
each extracted spec path is recorded as one :class:`ScannedFailedE2E` row.
Re-ticking on the same post produces no new signal: the unique constraint
on ``(channel, slack_ts, spec_path)`` makes the ledger the dedup gate.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class ScannedFailedE2E(models.Model):
    """One ``(channel, slack_ts, spec_path)`` observation row.

    A failed-E2E post can list multiple failing specs as bullets; the
    scanner emits one signal per spec and persists one row per spec.
    """

    overlay = models.CharField(max_length=64, blank=True, default="")
    channel = models.CharField(max_length=64)
    slack_ts = models.CharField(max_length=64)
    spec_path = models.CharField(max_length=512)
    test_title = models.CharField(max_length=512, blank=True, default="")
    observed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_scanned_failed_e2e"
        ordering: ClassVar = ["-observed_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["channel", "slack_ts", "spec_path"],
                name="uniq_failede2e_channel_ts_spec",
            ),
        ]

    def __str__(self) -> str:
        return f"failed-e2e<{self.pk}:{self.channel}/{self.slack_ts}:{self.spec_path}>"

    @classmethod
    def record(
        cls,
        *,
        channel: str,
        slack_ts: str,
        spec_path: str,
        test_title: str = "",
        overlay: str = "",
    ) -> "ScannedFailedE2E | None":
        """Insert idempotently; return the new row or ``None`` on dup."""
        if not channel or not slack_ts or not spec_path:
            return None
        row, created = cls.objects.get_or_create(
            channel=channel,
            slack_ts=slack_ts,
            spec_path=spec_path,
            defaults={"overlay": overlay, "test_title": test_title},
        )
        return row if created else None
