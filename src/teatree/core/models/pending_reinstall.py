"""Deferred editable-reinstall queue for the self-update scanner.

The self-update scanner (:class:`teatree.loop.scanners.self_update.SelfUpdateScanner`)
fast-forwards an editable clone every tick, but a ``git pull`` alone does
not re-anchor the *running* interpreter on the new code — that needs ``uv
tool install --editable <src> --reinstall`` + ``t3 setup`` + a self-DB
migrate, which the scanner deliberately never runs inline (it would steal
the foreground mid-tick).

``PendingReinstall`` is the bridge: when ``auto_update_reinstall`` is
enabled and the scanner advances a clone, it upserts one row per
``repo_label`` recording the SHA it pulled to. The deferred drain
(:mod:`teatree.loop.self_update_reinstall`) runs as the very first step of
the next per-tick subprocess — a fresh process, before any scanner code
imports — and applies the reinstall there, so the mutation never happens
in a process that already imported the old code (no mixed-code window).

One row per ``repo_label`` (the same identity key the scanner's
:class:`SelfUpdateMarker` uses). A re-pull before the drain runs upserts
the row back to ``pending`` with the new target SHA; the drain marks it
``done`` / ``failed`` once it executes.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class PendingReinstallManager(models.Manager["PendingReinstall"]):
    def upsert_pending(self, *, repo_label: str, target_sha: str) -> "PendingReinstall":
        """Record (or reset to) a pending reinstall for *repo_label* at *target_sha*."""
        row, _ = self.update_or_create(
            repo_label=repo_label,
            defaults={
                "target_sha": target_sha,
                "state": PendingReinstall.State.PENDING,
                "requested_at": timezone.now(),
                "attempts": 0,
                "last_error": "",
            },
        )
        return row

    def pending(self) -> models.QuerySet["PendingReinstall"]:
        return self.filter(state=PendingReinstall.State.PENDING).order_by("requested_at", "pk")


class PendingReinstall(models.Model):
    """One deferred editable-reinstall request keyed on ``repo_label``."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    repo_label = models.CharField(max_length=64, unique=True)
    target_sha = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=8, choices=State.choices, default=State.PENDING)
    requested_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=200, blank=True, default="")

    objects: ClassVar[PendingReinstallManager] = PendingReinstallManager()

    class Meta:
        db_table = "teatree_pending_reinstall"
        ordering: ClassVar = ["requested_at"]

    def __str__(self) -> str:
        return f"pending-reinstall<{self.repo_label}:{self.state}@{self.target_sha[:8]}>"

    def mark_done(self) -> None:
        self.state = self.State.DONE
        self.attempts += 1
        self.last_error = ""
        self.save(update_fields=["state", "attempts", "last_error"])

    def mark_failed(self, error: str) -> None:
        self.state = self.State.FAILED
        self.attempts += 1
        self.last_error = error[:200]
        self.save(update_fields=["state", "attempts", "last_error"])
