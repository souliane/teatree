"""Durable cooldown parking a review backend that ran out of quota.

A backend whose account is exhausted fails every review it is handed, and it
fails them EXPENSIVELY: the `auto` reviewer-backend resolution would re-probe on
every tick, burning a dispatch per tick to rediscover the same exhaustion. The
cooldown is the memory that makes `auto` stop asking — it records the exhaustion
once, and resolution routes to the other backend until the row expires.

Expiry is a stored timestamp rather than a "cleared" flag so a forgotten reset can
never park a backend forever: the cooldown ends by the clock passing it, needing
no actor. Starting a fresh cooldown for a backend already cooling EXTENDS the same
row (there is at most one per backend+overlay), so repeated exhaustion pushes the
expiry out instead of stacking rows.

The signature that tripped it is stored for the audit trail: "which phrase, on
which run, parked this backend" is answerable from the row alone.
"""

from datetime import timedelta
from typing import ClassVar

from django.db import models
from django.utils import timezone


class ReviewBackendCooldown(models.Model):
    """One "this review backend is out of capacity until X" record."""

    backend = models.CharField(max_length=32)
    overlay = models.CharField(max_length=64, blank=True, default="")
    signature = models.CharField(max_length=128, blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "teatree_review_backend_cooldown"
        ordering: ClassVar = ["-expires_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["backend", "overlay"],
                name="uniq_reviewbackendcooldown_backend_overlay",
            ),
        ]

    def __str__(self) -> str:
        return f"review-backend-cooldown<{self.backend}@{self.overlay or '*'} until {self.expires_at.isoformat()}>"

    @property
    def is_live(self) -> bool:
        return self.expires_at > timezone.now()

    @classmethod
    def start(
        cls,
        *,
        backend: str,
        overlay: str = "",
        signature: str = "",
        ttl_hours: int,
    ) -> "ReviewBackendCooldown":
        """Park *backend* for *ttl_hours*, extending an existing cooldown rather than stacking."""
        started = timezone.now()
        return cls.objects.update_or_create(
            backend=backend,
            overlay=overlay,
            defaults={
                "signature": signature,
                "started_at": started,
                "expires_at": started + timedelta(hours=ttl_hours),
            },
        )[0]

    @classmethod
    def is_cooling(cls, *, backend: str, overlay: str = "") -> bool:
        """Whether *backend* is parked for *overlay* right now.

        An overlay with no cooldown of its own falls back to the global (unscoped)
        row, so an exhaustion recorded before any overlay attribution still parks
        the backend everywhere rather than silently applying nowhere.
        """
        scopes = [overlay, ""] if overlay else [""]
        return cls.objects.filter(backend=backend, overlay__in=scopes, expires_at__gt=timezone.now()).exists()
