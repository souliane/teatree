"""Singleton cadence/rate-limit/hysteresis ledger for the resource-pressure scanner.

The :class:`ResourcePressureScanner` runs every loop tick but only
*measures* once per ``resource_pressure_cadence_minutes`` and only runs a
freeing pass once per ``resource_pressure_min_free_interval_minutes`` —
both gates are carried across tick boundaries by this durable row. Without
it a sub-minute tick cadence would re-shell ``df``/``vm_stat`` (and worse,
re-run cache purges) on every tick.

There is one logical row (``singleton=True``, a unique boolean). Mirrors
:class:`SelfUpdateMarker`: the scanner upserts after each measurement pass
(swallow-and-continue on any DB error) so the cadence/rate-limit gates can
short-circuit cheaply on the next tick. ``last_plan`` records the dry-run
plan + reclaimed bytes from the most recent freeing pass so the user can
see what the scanner did (or *would have* done, when destructive flags are
off) — satisfying the done-claims-require-artifact-evidence contract.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class ResourcePressureMarker(models.Model):
    """One singleton row carrying the cadence/rate-limit/hysteresis state.

    ``last_run_at`` gates the measurement cadence; ``last_freed_at`` gates
    the freeing-pass rate-limit (anti-thrash). ``consecutive_critical``
    counts back-to-back CRITICAL-RAM ticks so the flag-gated process-kill
    escalation only fires after a sustained episode. ``last_warn_dm_at``
    dedups the WARN-band advisory DM to once per day. ``last_plan`` is the
    human-readable dry-run plan + reclaimed bytes of the most recent
    freeing pass.
    """

    singleton = models.BooleanField(default=True, unique=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_disk_free_gb = models.FloatField(null=True, blank=True)
    last_ram_avail_gb = models.FloatField(null=True, blank=True)
    last_freed_at = models.DateTimeField(null=True, blank=True)
    consecutive_critical = models.IntegerField(default=0)
    last_warn_dm_at = models.DateTimeField(null=True, blank=True)
    last_plan = models.TextField(blank=True, default="")

    class Meta:
        db_table = "teatree_resource_pressure_marker"
        ordering: ClassVar = ["-last_run_at"]

    def __str__(self) -> str:
        return f"resource-pressure<disk={self.last_disk_free_gb}gb ram={self.last_ram_avail_gb}gb>"

    @classmethod
    def load(cls) -> "ResourcePressureMarker":
        """Return the singleton row, creating it on first access."""
        marker, _ = cls.objects.get_or_create(singleton=True)
        return marker

    def record_measurement(self, *, disk_free_gb: float, ram_avail_gb: float) -> None:
        self.last_run_at = timezone.now()
        self.last_disk_free_gb = disk_free_gb
        self.last_ram_avail_gb = ram_avail_gb
        self.save(update_fields=["last_run_at", "last_disk_free_gb", "last_ram_avail_gb"])
