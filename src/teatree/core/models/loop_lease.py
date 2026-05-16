"""DB lease/heartbeat for machine-wide loop ownership (#786 WS2).

Replaces the ``utils.singleton`` flock/pidfile loop-owner guard. A flock
dies with its process and is invisible to any other process; a DB lease
row is queryable, reapable by expiry, and survives context compaction
(the loop-owner can re-acquire after a compaction instead of the
ownership silently vanishing).

Acquisition is a backend-agnostic atomic compare-and-swap — a single
conditional ``UPDATE ... WHERE (unowned OR lease expired)`` — NOT
``select_for_update(skip_locked=True)``: teatree's production DB is
SQLite, where ``has_select_for_update_skip_locked`` is ``False`` and that
clause is silently dropped (the #786 B1 lesson). Exactly one of N
concurrent ticks wins the CAS; the losers see 0 rows updated and skip.
"""

from datetime import timedelta

from django.db import models
from django.utils import timezone

from teatree.core.managers import LoopLeaseManager

_DEFAULT_LEASE_SECONDS = 120


class LoopLease(models.Model):
    """One row per named machine-wide loop (e.g. ``loop-tick``)."""

    name = models.CharField(max_length=128, unique=True)
    owner = models.CharField(max_length=255, blank=True)
    acquired_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    objects = LoopLeaseManager()

    class Meta:
        db_table = "teatree_loop_lease"

    def __str__(self) -> str:
        return f"loop-lease<{self.name} owner={self.owner or '-'}>"

    @property
    def is_held(self) -> bool:
        """True iff the lease is owned and not yet expired."""
        if not self.owner or self.lease_expires_at is None:
            return False
        return self.lease_expires_at > timezone.now()

    def renew(self, *, lease_seconds: int = _DEFAULT_LEASE_SECONDS) -> None:
        """Extend the lease for the current owner (heartbeat)."""
        now = timezone.now()
        self.heartbeat_at = now
        self.lease_expires_at = now + timedelta(seconds=lease_seconds)
        self.save(update_fields=["heartbeat_at", "lease_expires_at"])
