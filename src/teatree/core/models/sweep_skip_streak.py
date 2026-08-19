"""Consecutive-skip ledger for the PR sweep — the durable half of aged-skip surfacing.

``PrSweepScanner`` declines to merge on ~10 reasons (``no_clear_for_head``,
``ci_pending``, ``ci_red``, ``required_checks_indeterminate``, ``draft``,
``changes_requested``, ``needs_branch_update``, ``solo_overlay_no_review``,
``keystone_refused``, fork/untrusted provenance). Each is a sound per-tick decision
and each is log-only, so a PR that is skipped every tick forever produces no signal
at all — the shape where a finished branch quietly never merges.

One row per ``(slug, pr_id)`` counts how many CONSECUTIVE sweep passes produced the
same reason GROUP. Persistence is what surfaces, not any individual skip: a reason
from a different group restarts the count (a different problem), a non-skip outcome
deletes the row (the PR moved), and ``surfaced_at`` — the last time this PR was
announced, independent of which reason triggered it — gates a cooldown so a PR held
for days is announced once, then again only after backing off, rather than every tick.

``classify_sweep_ci`` emits four reasons — ``required_checks_indeterminate``,
``ci_pending``, ``uv_audit_red_but_clean_on_main``, ``ci_red`` — for the ONE condition
"this PR's checks are not clean yet", and a PR's verdict legitimately alternates between
them on consecutive passes. Counting each flip as a new problem reset the run length
every tick, so the flappiest PRs — the ones most worth announcing — could never reach
the surface threshold at all (#4095). They are therefore one group for streak continuity
only: the stored ``reason`` still tracks the LATEST observation while ``first_seen_at``
holds at the group's start, so the doctor view and the DM pair the actionable current
reason with the age of the condition rather than of that one flip. A within-group flip
never touches ``surfaced_at`` — re-arming is the cooldown window's job alone (#4086).
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

from django.db import models
from django.utils import timezone

_SECONDS_PER_HOUR = 3600
_STREAK_FIELDS = ("reason", "url", "overlay", "first_seen_at", "last_seen_at", "tick_count", "surfaced_at")

#: The reasons ``teatree.loop.scanners.pr_sweep_decision.classify_sweep_ci`` can emit —
#: one condition observed at four moments. See the module docstring.
_CI_VERDICT_REASONS = frozenset(
    {"required_checks_indeterminate", "ci_pending", "uv_audit_red_but_clean_on_main", "ci_red"},
)
_CI_VERDICT_GROUP = "group:ci_verdict"

#: Skip reasons that accrue a streak — and stay in :meth:`SweepSkipStreakManager.aged`,
#: the doctor's standing view — but never trigger the owner-escalation DM. ``draft`` is
#: a deliberate park, usually because the owner asked to read the PR before merge, not
#: a stall; alarming on it turns a park into a false escalation every reannounce window.
_NEVER_ANNOUNCE_REASONS = frozenset({"draft"})


def _reason_group(reason: str) -> str:
    """The identity a streak continues on: the CI verdict collapses, everything else stands alone."""
    return _CI_VERDICT_GROUP if reason in _CI_VERDICT_REASONS else reason


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
            row.first_seen_at = moment
            row.tick_count = 1
        else:
            row.tick_count += 1
        row.reason = observation.reason
        row.url = observation.url or row.url
        row.overlay = observation.overlay or row.overlay
        row.last_seen_at = moment
        row.save(update_fields=list(_STREAK_FIELDS))
        return row

    def resolve(self, *, slug: str, pr_id: int) -> int:
        """Drop the streak — the PR produced a non-skip outcome. Returns rows removed."""
        deleted, _ = self.filter(slug=slug, pr_id=pr_id).delete()
        return deleted

    def purge_absent(self, *, slug: str, seen_pr_ids: Iterable[int]) -> int:
        """Drop *slug*'s streak rows whose PR is absent from *seen_pr_ids*. Returns rows removed.

        *seen_pr_ids* is the full open-PR set from one successful ``pr_sweep.pass``
        listing. A streak row not in it left the open set — merged or closed — which is
        a finished fact, not a stall, so it is dropped rather than re-announced forever.
        """
        deleted, _ = self.filter(slug=slug).exclude(pr_id__in=list(seen_pr_ids)).delete()
        return deleted

    def due_to_surface(
        self,
        *,
        threshold: int,
        cooldown: dt.timedelta,
        now: dt.datetime | None = None,
    ) -> models.QuerySet["SweepSkipStreak"]:
        """Streaks at/over *threshold* that have never surfaced, or last surfaced ≥ *cooldown* ago.

        Reason-independent: a PR that has already been announced does not become due
        again just because its granular skip reason changed — only the backoff window
        re-arms it, so a flapping ``ci_red``/``ci_pending`` on the same stuck PR is one
        notification and a later reminder, not one notification per wobble. Excludes
        :data:`_NEVER_ANNOUNCE_REASONS` (``draft``): the row still accrues and still
        shows in :meth:`aged`, only the DM never fires for it.
        """
        moment = now or timezone.now()
        never_surfaced = models.Q(surfaced_at__isnull=True)
        cooled_down = models.Q(surfaced_at__lte=moment - cooldown)
        return (
            self.filter(tick_count__gte=threshold)
            .exclude(reason__in=_NEVER_ANNOUNCE_REASONS)
            .filter(never_surfaced | cooled_down)
            .order_by("first_seen_at")
        )

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
