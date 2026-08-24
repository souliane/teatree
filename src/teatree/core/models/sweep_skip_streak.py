"""Consecutive-skip ledger for the PR sweep — the durable half of aged-skip surfacing.

``PrSweepScanner`` declines to merge on ~10 reasons (``no_clear_for_head``,
``ci_pending``, ``ci_red``, ``required_checks_indeterminate``, ``draft``,
``changes_requested``, ``needs_branch_update``, ``solo_overlay_no_review``,
``keystone_refused``, fork/untrusted provenance). Each is a sound per-tick decision
and each is log-only, so a PR that is skipped every tick forever produces no signal
at all — the shape where a finished branch quietly never merges.

One row per ``(slug, pr_id)`` counts how many CONSECUTIVE sweep passes produced the
same reason GROUP. Persistence is what surfaces, not any individual skip: a reason
from a different group restarts the count (a different problem), a MERGE deletes the
row (the PR moved — every other outcome is one more pass on which it did NOT land, so
``keystone_refused`` and the ``flag_*`` family accrue here too), and ``surfaced_at`` —
the last time this PR was announced, independent of which reason triggered it — gates a
cooldown so a PR held for days is announced once, then again only after backing off,
rather than every tick.

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

Persistence is not the only question: WHAT persists decides whether anyone should be told.
:data:`SKIP_REASON_DISPOSITION` classifies every reason the sweep can emit as a deliberate
park or a stall, in one place, so a new reason cannot join the alarm set by accident (#4523).
"""

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar

from django.db import models
from django.utils import timezone

_SECONDS_PER_HOUR = 3600
_STREAK_FIELDS = ("reason", "url", "overlay", "first_seen_at", "last_seen_at", "tick_count", "surfaced_at")
_STANDING_DETAIL_LIMIT = 3

#: The reasons ``teatree.loop.scanners.pr_sweep_decision.classify_sweep_ci`` can emit —
#: one condition observed at four moments. See the module docstring.
_CI_VERDICT_REASONS = frozenset(
    {"required_checks_indeterminate", "ci_pending", "uv_audit_red_but_clean_on_main", "ci_red"},
)
_CI_VERDICT_GROUP = "group:ci_verdict"


def _reason_group(reason: str) -> str:
    """The identity a streak continues on: the CI verdict collapses, everything else stands alone."""
    return _CI_VERDICT_GROUP if reason in _CI_VERDICT_REASONS else reason


class SkipDisposition(StrEnum):
    """Whether a skip reason names a state someone CHOSE, or one nobody did."""

    DELIBERATE_PARK = "deliberate_park"
    STALL = "stall"


#: Every skip reason ``pr_sweep`` can emit, classified once. A ``draft`` PR is parked on
#: purpose — usually because the owner asked to read it before it merges — so no run length
#: makes it an alarm. A fork/untrusted hold stays a STALL: nobody chose it and it is exactly
#: what needs the owner. ``TestEverySkipReasonIsClassified`` reds until a new reason is here.
SKIP_REASON_DISPOSITION: Mapping[str, SkipDisposition] = MappingProxyType(
    {
        "draft": SkipDisposition.DELIBERATE_PARK,
        "changes_requested": SkipDisposition.STALL,
        "fork_requires_human_approval": SkipDisposition.STALL,
        "untrusted_author_public_repo": SkipDisposition.STALL,
        "required_checks_indeterminate": SkipDisposition.STALL,
        "ci_pending": SkipDisposition.STALL,
        "uv_audit_red_but_clean_on_main": SkipDisposition.STALL,
        "ci_red": SkipDisposition.STALL,
        "no_clear_for_head": SkipDisposition.STALL,
    },
)

DELIBERATE_PARK_REASONS = frozenset(
    reason for reason, disposition in SKIP_REASON_DISPOSITION.items() if disposition is SkipDisposition.DELIBERATE_PARK
)


def disposition_for(reason: str) -> SkipDisposition:
    """An unclassified reason falls to STALL — silence about a real stall is the worse failure."""
    return SKIP_REASON_DISPOSITION.get(reason, SkipDisposition.STALL)


@dataclass(frozen=True, slots=True)
class StandingSkips:
    """The aged ledger as ONE finding: the counts, the age, and the few worth naming."""

    stalls: int
    parks: int
    oldest_age_label: str
    worst: tuple["SweepSkipStreak", ...]

    @property
    def total(self) -> int:
        return self.stalls + self.parks


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
        """Drop the streak — the PR LANDED. Returns rows removed."""
        deleted, _ = self.filter(slug=slug, pr_id=pr_id).delete()
        return deleted

    def _delete_pks(self, pks: list[int]) -> int:
        if not pks:
            return 0
        deleted, _ = self.filter(pk__in=pks).delete()
        return deleted

    def drop_terminal(self, *, terminal_refs: Iterable[tuple[str, int]]) -> int:
        """Discard the streaks of PRs *terminal_refs* proves settled. Returns rows removed.

        The count goes with the row: a reopened PR is a fresh condition, not the
        continuation of the one that closed. Slugs fold to lower case, the rule
        :meth:`~teatree.core.models.pull_request.PullRequestQuerySet.for_pr` states —
        matching ``Owner/Repo`` exactly against ``owner/repo`` drops every such row from
        consideration and the fossil survives.
        """
        settled = {(slug.casefold(), pr_id) for slug, pr_id in terminal_refs}
        if not settled:
            return 0
        tracked = self.values_list("pk", "slug", "pr_id")
        return self._delete_pks([pk for pk, slug, pr_id in tracked if (slug.casefold(), pr_id) in settled])

    def drop_departed(self, *, slugs: Iterable[str], stale_before: dt.datetime) -> int:
        """Discard the streaks of PRs that left a swept slug's open set. Returns rows removed.

        The sweep enumerates OPEN PRs, so that enumeration IS the liveness oracle — and
        the only one with full coverage, since a PR opened outside the pipeline has no
        local row to read a state from. *stale_before* is the grace: ``scan()`` emits no
        signal for a PR whose evaluation raised, and one missed pass is not a departure.
        *slugs* bounds the sweep to what this pass actually enumerated, so a slug held by
        an unswept overlay — or by a forge read that failed — keeps its rows.
        """
        swept = {slug.casefold() for slug in slugs}
        if not swept:
            return 0
        unseen = self.filter(last_seen_at__lt=stale_before).values_list("pk", "slug")
        return self._delete_pks([pk for pk, slug in unseen if slug.casefold() in swept])

    def due_to_surface(
        self,
        *,
        threshold: int,
        cooldown: dt.timedelta,
        now: dt.datetime | None = None,
        observed_since: dt.datetime | None = None,
    ) -> models.QuerySet["SweepSkipStreak"]:
        """Streaks at/over *threshold* that have never surfaced, or last surfaced ≥ *cooldown* ago.

        Reason-independent: a PR that has already been announced does not become due
        again just because its granular skip reason changed — only the backoff window
        re-arms it, so a flapping ``ci_red``/``ci_pending`` on the same stuck PR is one
        notification and a later reminder, not one notification per wobble. A
        :data:`DELIBERATE_PARK_REASONS` streak never becomes due at all — it still accrues
        and still stands in :meth:`aged`; only the DM stops.

        *observed_since* bounds the answer to the rows the caller's own pass folded in.
        The announcement's claim — skipped N times running — is about the CURRENT pass,
        so a row that pass never touched cannot support it: PR #4055 closed and froze at
        57 while its age label kept growing, and the cooldown re-armed that dead finding
        daily for fifteen days (#4518).
        """
        moment = now or timezone.now()
        never_surfaced = models.Q(surfaced_at__isnull=True)
        cooled_down = models.Q(surfaced_at__lte=moment - cooldown)
        due = (
            self.filter(tick_count__gte=threshold)
            .exclude(reason__in=DELIBERATE_PARK_REASONS)
            .filter(never_surfaced | cooled_down)
        )
        if observed_since is not None:
            due = due.filter(last_seen_at__gte=observed_since)
        return due.order_by("first_seen_at")

    def aged(self, *, threshold: int) -> models.QuerySet["SweepSkipStreak"]:
        """Every streak at/over *threshold*, announced or not — the standing doctor view."""
        return self.filter(tick_count__gte=threshold).order_by("first_seen_at")

    def standing(
        self,
        *,
        threshold: int,
        limit: int = _STANDING_DETAIL_LIMIT,
        now: dt.datetime | None = None,
    ) -> StandingSkips:
        """The aged view folded into one finding — the count, not one item per row (#4523).

        ``worst`` names only stalls: a park is counted and then left alone, so the detail
        the reader acts on is never crowded out by PRs somebody parked on purpose.
        """
        rows = list(self.aged(threshold=threshold))
        stalls = [row for row in rows if disposition_for(row.reason) is SkipDisposition.STALL]
        return StandingSkips(
            stalls=len(stalls),
            parks=len(rows) - len(stalls),
            oldest_age_label=rows[0].age_label(now=now) if rows else "",
            worst=tuple(stalls[:limit]),
        )

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

    @property
    def link(self) -> str:
        """What every surface renders for this PR — the url, else the ref (#4518).

        A row predating the sweep stamping its skips carries no url, and the literal
        that stood in for one told the reader nothing they could act on.
        """
        return self.url or self.ref

    def age(self, *, now: dt.datetime | None = None) -> dt.timedelta:
        return (now or timezone.now()) - self.first_seen_at

    def age_label(self, *, now: dt.datetime | None = None) -> str:
        """Whole hours since the first sighting, e.g. ``5h``."""
        return f"{int(self.age(now=now).total_seconds() // _SECONDS_PER_HOUR)}h"
