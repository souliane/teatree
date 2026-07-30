"""Durable evidence that a checkout held work existing nowhere else.

The on-disk salvage bundle is only as durable as the directory it sits in, and
nothing queries a directory. This row is what makes the capture observable: one
record per checkout, written BEFORE any reaping pass may act on it, carrying the
dirty paths, the unpushed commits, and the artifact prefix the bundle was written
under.
"""

from typing import ClassVar

from django.db import models


class UnshippedWorkRecord(models.Model):
    # The idempotency key: a re-capture of the same checkout updates its one row.
    checkout_path = models.CharField(max_length=1024, unique=True)
    branch = models.CharField(max_length=255, blank=True)
    overlay = models.CharField(max_length=100, blank=True)
    dirty_paths = models.JSONField(default=list)
    unpushed_commits = models.JSONField(default=list)
    artifact_prefix = models.CharField(max_length=1024, blank=True)
    unreadable = models.TextField(blank=True)
    captured_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teatree_unshipped_work_record"
        ordering: ClassVar = ["-captured_at"]

    def __str__(self) -> str:
        return f"unshipped<{self.checkout_path} dirty={len(self.dirty_paths)} ahead={len(self.unpushed_commits)}>"
