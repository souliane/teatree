from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from django.db import transaction
from django.utils import timezone

from teatree.core.modelkit.owner_action import MARKER_PREFIX
from teatree.core.models import ConsolidatedMemory, DeferredQuestion, Ticket
from teatree.core.models.dream_gap_ledger import batch_entries, pending_entries
from teatree.core.models.dream_gap_types import DreamFunnelEntry
from teatree.loops.dream.gap_coverage import gap_coverage
from teatree.loops.dream.stranded_hosts import stranded_host_count

if TYPE_CHECKING:
    from teatree.loops.dream.batch_promote import PromotionBatch

FUNNEL_RETENTION_DAYS = 120
FIXED_ZERO_CARD_DAYS = 7


@dataclass(frozen=True, slots=True)
class QuestionClassStatus:
    name: str
    fixed: bool
    daily_counts: tuple[int, ...]
    last_generated_at: datetime


def question_class(marker: str) -> str:
    pieces = marker.split(":")
    if marker.startswith(MARKER_PREFIX) and len(pieces) > 1:
        return ":".join(pieces[:2])
    return pieces[0]


def _question_status(name: str, created: list[datetime], now: datetime) -> QuestionClassStatus | None:
    if not created:
        return None
    today = now.astimezone(UTC).date()
    days = [today - timedelta(days=offset) for offset in range(FIXED_ZERO_CARD_DAYS, 0, -1)]
    counts = tuple(sum(stamp.astimezone(UTC).date() == day for stamp in created) for day in days)
    last = max(created)
    return QuestionClassStatus(name, last.astimezone(UTC).date() < days[0], counts, last)


def question_class_status(name: str, *, now: datetime | None = None) -> QuestionClassStatus | None:
    created = [
        row.created_at
        for row in DeferredQuestion.objects.exclude(dedupe_marker="").only("dedupe_marker", "created_at")
        if question_class(row.dedupe_marker) == name
    ]
    return _question_status(name, created, now or timezone.now())


def question_classes(*, now: datetime | None = None) -> list[QuestionClassStatus]:
    by_class: dict[str, list[datetime]] = defaultdict(list)
    for row in DeferredQuestion.objects.exclude(dedupe_marker="").only("dedupe_marker", "created_at"):
        by_class[question_class(row.dedupe_marker)].append(row.created_at)
    current = now or timezone.now()
    return [status for name in sorted(by_class) if (status := _question_status(name, by_class[name], current))]


def _snapshot(*, umbrella: Ticket, batch: "PromotionBatch", at: datetime) -> DreamFunnelEntry:
    dispositions = ConsolidatedMemory.Disposition
    hosts = Ticket.objects.exclude(extra__dream_gap_batch__isnull=True).exclude(state=Ticket.State.IGNORED)
    folded = addressed = rejected = 0
    for host in hosts:
        folded += len(batch_entries(host))
        for record in ((host.extra or {}).get("dream_gap_dispositions") or {}).values():
            if not isinstance(record, dict):
                continue
            addressed += record.get("disposition") == "address"
            rejected += record.get("disposition") == "reject"
    return {
        "at": at.isoformat(),
        "rows": ConsolidatedMemory.objects.count(),
        "kept": ConsolidatedMemory.objects.filter(disposition=dispositions.USER_SPECIFIC_KEEP).count(),
        "core_gaps": ConsolidatedMemory.objects.filter(
            disposition__in=[dispositions.CORE_GAP_NEEDS_TICKET, dispositions.TICKETED, dispositions.RESOLVED_RETIRED]
        ).count(),
        "withheld": batch.withheld,
        "pending": len(pending_entries(umbrella)),
        "folded": folded,
        "addressed": addressed,
        "rejected": rejected,
        "retired": ConsolidatedMemory.objects.filter(disposition=dispositions.RESOLVED_RETIRED).count(),
        "stranded_hosts": stranded_host_count(at),
        "orphans": len(gap_coverage(umbrella_url=umbrella.issue_url).orphan),
    }


def record_funnel(*, umbrella_url: str, batch: "PromotionBatch", now: datetime | None = None) -> DreamFunnelEntry:
    at = now or timezone.now()
    cutoff = at - timedelta(days=FUNNEL_RETENTION_DAYS)
    with transaction.atomic():
        umbrella = Ticket.objects.select_for_update().filter(issue_url=umbrella_url).first()
        if umbrella is None:
            msg = f"no dream umbrella ticket at {umbrella_url}"
            raise ValueError(msg)
        snapshot = _snapshot(umbrella=umbrella, batch=batch, at=at)
        kept: list[DreamFunnelEntry] = [
            cast("DreamFunnelEntry", entry)
            for entry in (umbrella.extra or {}).get("dream_funnel") or []
            if isinstance(entry, dict)
            and isinstance(entry.get("at"), str)
            and datetime.fromisoformat(entry["at"]) >= cutoff
        ]
        if snapshot not in kept:
            kept.append(snapshot)
        umbrella.merge_extra(set_keys={"dream_funnel": kept})
        umbrella.refresh_from_db(fields=["extra"])
        if snapshot not in (umbrella.extra or {}).get("dream_funnel", []):
            msg = "dream funnel snapshot did not persist"
            raise RuntimeError(msg)
    return snapshot


def daily_funnel(umbrella_url: str) -> list[DreamFunnelEntry]:
    umbrella = Ticket.objects.filter(issue_url=umbrella_url).first()
    if umbrella is None:
        return []
    by_day: dict[str, DreamFunnelEntry] = {}
    for entry in (umbrella.extra or {}).get("dream_funnel") or []:
        if isinstance(entry, dict) and isinstance(entry.get("at"), str):
            day = entry["at"][:10]
            prior = by_day.get(day)
            if prior is None or entry["at"] > prior["at"]:
                by_day[day] = cast("DreamFunnelEntry", entry)
    return [by_day[day] for day in sorted(by_day)]
