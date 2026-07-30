r"""Cached per-account Anthropic rate-limit health, keyed by ``pass`` entry.

The health cache the routing selector (``teatree.credential_config``) reads on the
HOT path instead of the network: one row per ``pass_path`` records the account's
last-probed unified 5h / 7d utilization + status + reset, when it was
:attr:`checked_at`, and how long the verdict is trusted (:attr:`valid_until`). The
selector reuses a fresh, non-exhausted row with NO probe; it re-probes and
:meth:`AnthropicTokenUsageManager.record`\ s a fresh row only on a cache miss /
expiry.

This module is DOMAIN and stays free of ``teatree.llm``: :meth:`record` takes a
:class:`TokenHealthReading` value object (the already-parsed primitive fields), not a
``RateLimitSnapshot`` — the selector (which knows both the reader and this cache)
builds the reading at the boundary. The :attr:`valid_until` policy lives HERE so it
has one home: a healthy verdict expires after :data:`HEALTH_TTL` (re-probe
occasionally), an exhausted one is trusted until its blocking window(s) reset (so an
exhausted account is NOT re-probed until it can free up).
"""

import datetime as dt
from dataclasses import dataclass
from typing import ClassVar

from django.db import models
from django.utils import timezone

UTILIZATION_5H_LIMIT = 0.95
UTILIZATION_7D_LIMIT = 0.99
REJECTED_STATUS = "rejected"
HEALTH_TTL = dt.timedelta(minutes=5)


def _is_exhausted(utilization_5h: float, utilization_7d: float, status_7d: str) -> bool:
    """The exhaustion rule, shared by the model, the reading, and the ``valid_until`` policy."""
    return (
        utilization_5h >= UTILIZATION_5H_LIMIT or utilization_7d >= UTILIZATION_7D_LIMIT or status_7d == REJECTED_STATUS
    )


def _blocking_resets(
    *,
    utilization_5h: float,
    utilization_7d: float,
    status_7d: str,
    reset_5h: dt.datetime | None,
    reset_7d: dt.datetime | None,
) -> list[dt.datetime]:
    """The resets of the windows currently BLOCKING an account.

    A window only blocks when that window is itself spent: an idle 5h window does NOT hold
    the account back even though its reset is the sooner of the two. Both :meth:`valid_until`
    (when to re-probe) and :attr:`AnthropicTokenUsage.frees_up_at` (when the account re-arms)
    read this one rule, so they can never disagree about which window matters.
    """
    blocking: list[dt.datetime] = []
    if utilization_5h >= UTILIZATION_5H_LIMIT and reset_5h is not None:
        blocking.append(reset_5h)
    if (utilization_7d >= UTILIZATION_7D_LIMIT or status_7d == REJECTED_STATUS) and reset_7d is not None:
        blocking.append(reset_7d)
    return blocking


@dataclass(frozen=True)
class TokenHealthReading:
    """One account's parsed rate-limit fields, handed to the cache at the boundary.

    The DOMAIN-side twin of ``teatree.llm.rate_limits.RateLimitSnapshot`` (minus the
    token-signing concern): the selector translates a snapshot into this so the cache
    never imports the foundation reader.
    """

    organization_id: str
    utilization_5h: float
    utilization_7d: float
    status_5h: str
    status_7d: str
    reset_5h: dt.datetime | None
    reset_7d: dt.datetime | None

    @property
    def is_exhausted(self) -> bool:
        return _is_exhausted(self.utilization_5h, self.utilization_7d, self.status_7d)

    def valid_until(self, now: dt.datetime) -> dt.datetime:
        """When this reading's verdict stops being trusted.

        Healthy: the sooner of ``now + HEALTH_TTL`` and the nearest window reset, so a
        healthy token re-probes occasionally. Exhausted: the LATEST reset among the
        windows currently blocking it (all must clear before it frees up), so it is not
        re-probed until then; ``HEALTH_TTL`` is the floor when no reset is known.
        """
        ttl_bound = now + HEALTH_TTL
        if not self.is_exhausted:
            resets = [reset for reset in (self.reset_5h, self.reset_7d) if reset is not None]
            return min([ttl_bound, *resets]) if resets else ttl_bound
        blocking = _blocking_resets(
            utilization_5h=self.utilization_5h,
            utilization_7d=self.utilization_7d,
            status_7d=self.status_7d,
            reset_5h=self.reset_5h,
            reset_7d=self.reset_7d,
        )
        return max(blocking) if blocking else ttl_bound


class AnthropicTokenUsageManager(models.Manager["AnthropicTokenUsage"]):
    """Upsert helper for the per-``pass_path`` health cache."""

    def record(
        self, pass_path: str, reading: TokenHealthReading, *, now: dt.datetime | None = None
    ) -> "AnthropicTokenUsage":
        """Upsert the health row for *pass_path* from a fresh probe's *reading*.

        Idempotent on the unique ``pass_path``: a re-probe updates the one row. The
        stored :attr:`valid_until` follows the reading's TTL/reset policy so a healthy
        token re-probes after :data:`HEALTH_TTL` and an exhausted one waits out its reset.
        """
        moment = now or timezone.now()
        row, _ = self.update_or_create(
            pass_path=pass_path,
            defaults={
                "organization_id": reading.organization_id,
                "utilization_5h": reading.utilization_5h,
                "utilization_7d": reading.utilization_7d,
                "status_5h": reading.status_5h,
                "status_7d": reading.status_7d,
                "reset_5h": reading.reset_5h,
                "reset_7d": reading.reset_7d,
                "checked_at": moment,
                "valid_until": reading.valid_until(moment),
            },
        )
        return row


class AnthropicTokenUsage(models.Model):
    """One ``pass`` account's cached unified rate-limit health.

    Keyed by the unique :attr:`pass_path` (the credential's routed ``pass`` entry).
    :attr:`is_exhausted` is the routing verdict; :meth:`is_fresh` gates whether the
    cache may be trusted without a re-probe.
    """

    pass_path = models.CharField(max_length=255, unique=True)
    organization_id = models.CharField(max_length=255, blank=True, default="")
    utilization_5h = models.FloatField(default=0.0)
    utilization_7d = models.FloatField(default=0.0)
    status_5h = models.CharField(max_length=64, blank=True, default="")
    status_7d = models.CharField(max_length=64, blank=True, default="")
    reset_5h = models.DateTimeField(null=True, blank=True)
    reset_7d = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField()

    objects: ClassVar[AnthropicTokenUsageManager] = AnthropicTokenUsageManager()

    class Meta:
        db_table = "teatree_anthropic_token_usage"
        ordering: ClassVar = ["pass_path"]

    def __str__(self) -> str:
        return f"anthropic-usage<{self.pass_path} 5h={self.utilization_5h:.2f} 7d={self.utilization_7d:.2f}>"

    @property
    def is_exhausted(self) -> bool:
        """Whether this account is spent: 5h ≥ 95 %, 7d ≥ 99 %, or a rejected 7d window."""
        return _is_exhausted(self.utilization_5h, self.utilization_7d, self.status_7d)

    def is_fresh(self, now: dt.datetime | None = None) -> bool:
        """Whether the cached verdict is still trusted (``valid_until`` in the future)."""
        return self.valid_until > (now or timezone.now())

    @property
    def earliest_reset(self) -> dt.datetime | None:
        """The soonest window reset on record, or ``None`` when neither is known.

        A display read (the dash health band). Callers deciding WHEN AN EXHAUSTED ACCOUNT
        RE-ARMS must use :attr:`frees_up_at` instead — this one ignores which window is
        actually blocking and so can point at an idle window's imminent reset.
        """
        resets = [reset for reset in (self.reset_5h, self.reset_7d) if reset is not None]
        return min(resets) if resets else None

    @property
    def frees_up_at(self) -> dt.datetime | None:
        """When this account re-arms — the LATEST reset among the windows blocking it.

        Every blocking window must clear before the account is usable again, so this is a
        ``max``, not a ``min``: an account rejected on its 7-day window is not freed by its
        idle 5h window rolling over. ``None`` when the account is not blocked, or when no
        blocking window reported a reset — there is nothing to re-arm to, so a caller must
        not park behind it.
        """
        blocking = _blocking_resets(
            utilization_5h=self.utilization_5h,
            utilization_7d=self.utilization_7d,
            status_7d=self.status_7d,
            reset_5h=self.reset_5h,
            reset_7d=self.reset_7d,
        )
        return max(blocking) if blocking else None
