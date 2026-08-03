"""Consecutive-skip ledger for the PR sweep — the durable half of aged-skip surfacing.

``PrSweepScanner`` declines to merge on ~10 reasons (``no_clear_for_head``,
``ci_pending``, ``ci_red``, ``required_checks_indeterminate``, ``draft``,
``changes_requested``, ``needs_branch_update``, ``solo_overlay_no_review``,
``keystone_refused``, fork/untrusted provenance). Each is a sound per-tick decision
and each is log-only, so a PR that is skipped every tick forever produces no signal
at all — the shape where a finished branch quietly never merges.

One row per ``(slug, pr_id)`` counts how many CONSECUTIVE sweep passes produced the
SAME reason GROUP. Persistence is what surfaces, not any individual skip: a reason
that changes to a genuinely different GROUP restarts the count (a different
problem), a non-skip outcome deletes the row (the PR moved), and ``surfaced_at``
makes the surfacing single-shot so a PR held for days is announced once rather than
every tick.

``ci_pending``, ``ci_red``, ``required_checks_indeterminate`` and
``uv_audit_red_but_clean_on_main`` are all transient phases of the SAME CI-verdict
emitted by ``classify_sweep_ci`` — a PR's checks legitimately flap between pending
(still running) and red (a check failed, or reran) while it remains the SAME stuck
PR. Treating each flip as "a different problem" re-armed a fresh announcement every
time, producing back-to-back near-duplicate DMs for one stuck PR (#4080). These four
are grouped for the streak-continuity comparison only — the stored ``reason`` still
reflects the exact latest observation, so the surfaced text stays specific.
"""

import datetime as dt
from dataclasses import dataclass
from typing import ClassVar

from django.db import models
from django.utils import timezone

_SECONDS_PER_HOUR = 3600
_STREAK_FIELDS = ("reason", "url", "overlay", "first_seen_at", "last_seen_at", "tick_count", "surfaced_at")

#: Sub-reasons of ``classify_sweep_ci``'s CI-verdict — transient phases of one
#: "CI is not clean yet" condition, not distinct problems. See module docstring.
_CI_VERDICT_REASONS = frozenset(
    {"ci_pending", "ci_red", "required_checks_indeterminate", "uv_audit_red_but_clean_on_main"},
)


def _reason_group(reason: str) -> str:
    """The identity used to decide whether a streak continues or restarts.

    Collapses the CI-verdict sub-reasons to one group; every other reason (draft,
    changes_requested, fork holds, no_clear_for_head, …) is its own group.
    """
    return "ci_verdict" if reason in _CI_VERDICT_REASONS else reason


@dataclass(frozen=True, slots=True)
class SkipObservation:
    """One sweep pass's verdict for one PR — the manager's write unit."""

    slug: str
    pr_id: int
    reason: str
    url: str = ""
    overlay: str = ""


class SweepSkipStreakManager(models.Manager["SweepSkipStreak"]):
    """Observe / resolve / surface the consecutive-skip streaks."""

    def observe(self, observation: SkipObservation, *, now: dt.datetime | None = None) -> "SweepSkipStreak":
        """Record one skip pass; return the row with its updated streak."""
        moment = now or timezone.now()
        row, created = self.get_or_create(
            slug=observation.slug,
            pr_id=observation.pr_id,
            defaults={
                "reason": observation.reason,
                "url": observation.url,
                "overlay": observation.overlay,
                "first_seen_at": moment,
                "last_seen_at": moment,
                "tick_count": 1,
            },
        )
        if created:
            return row
        if _reason_group(row.reason) != _reason_group(observation.reason):
            row.reason = observation.reason
            row.first_seen_at = moment
            row.tick_count = 1
            row.surfaced_at = None
        else:
            row.reason = observation.reason
            row.tick_count += 1
        row.url = observation.url or row.url
        row.overlay = observation.overlay or row.overlay
        row.last_seen_at = moment
        row.save(update_fields=list(_STREAK_FIELDS))
        return row

    def resolve(self, *, slug: str, pr_id: int) -> int:
        """Drop the streak — the PR produced a non-skip outcome. Returns rows removed."""
        deleted, _ = self.filter(slug=slug, pr_id=pr_id).delete()
        return deleted

    def due_to_surface(self, *, threshold: int) -> models.QuerySet["SweepSkipStreak"]:
        """Streaks that have reached *threshold* and have not been announced yet."""
        return self.filter(tick_count__gte=threshold, surfaced_at__isnull=True).order_by("first_seen_at")

    def aged(self, *, threshold: int) -> models.QuerySet["SweepSkipStreak"]:
        """Every streak at/over *threshold*, announced or not — the standing doctor view."""
        return self.filter(tick_count__gte=threshold).order_by("first_seen_at")

    def mark_surfaced(self, pks: list[int], *, now: dt.datetime | None = None) -> int:
        return self.filter(pk__in=pks).update(surfaced_at=now or timezone.now())


class SweepSkipStreak(models.Model):
    """One PR's run of consecutive identical sweep skips."""

    overlay = models.CharField(max_length=64, blank=True, default="")
    slug = models.CharField(max_length=255)
    pr_id = models.IntegerField()
    reason = models.CharField(max_length=128)
    url = models.URLField(max_length=512, blank=True, default="")
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    tick_count = models.PositiveIntegerField(default=1)
    surfaced_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[SweepSkipStreakManager] = SweepSkipStreakManager()

    class Meta:
        db_table = "teatree_sweep_skip_streak"
        ordering: ClassVar = ["-last_seen_at"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["slug", "pr_id"], name="uniq_sweep_skip_streak_slug_pr"),
        ]

    def __str__(self) -> str:
        return f"sweep-skip<{self.slug}#{self.pr_id} {self.reason} x{self.tick_count}>"

    @property
    def ref(self) -> str:
        return f"{self.slug}#{self.pr_id}"

    def age(self, *, now: dt.datetime | None = None) -> dt.timedelta:
        return (now or timezone.now()) - self.first_seen_at

    def age_label(self, *, now: dt.datetime | None = None) -> str:
        """Whole hours since the first sighting, e.g. ``5h``."""
        return f"{int(self.age(now=now).total_seconds() // _SECONDS_PER_HOUR)}h"
