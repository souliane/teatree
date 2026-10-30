"""Elapsed PR time from the current admission stamp, then first transition or PR creation when missing."""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import F, Max

from teatree.core.models.pull_request import PullRequest
from teatree.core.models.task import Task


@dataclass(frozen=True, slots=True)
class OpenPullRequestAge:
    url: str
    age_seconds: float | None


def merged_pull_request_seconds(*, since: datetime, until: datetime, overlay: str = "") -> tuple[float, ...]:
    rows = PullRequest.objects.filter(state=PullRequest.State.MERGED).with_flow_stamps()
    if overlay:
        rows = rows.filter(overlay=overlay)
    rows = rows.filter(flow_merged_at__gte=since, flow_merged_at__lt=until)
    return tuple(
        (row.flow_merged_at - row.flow_started_at).total_seconds()
        for row in rows.iterator()
        if row.flow_started_at is not None and row.flow_merged_at >= row.flow_started_at
    )


def open_pull_requests(*, now: datetime, overlay: str = "") -> tuple[OpenPullRequestAge, ...]:
    rows = PullRequest.objects.live().with_flow_stamps()
    if overlay:
        rows = rows.filter(overlay=overlay)
    rows = rows.order_by(F("flow_started_at").asc(nulls_last=True), "pk")
    return tuple(
        OpenPullRequestAge(
            url=row.url,
            age_seconds=max(0.0, (now - row.flow_started_at).total_seconds())
            if row.flow_started_at is not None
            else None,
        )
        for row in rows.iterator()
    )


def phase_gap_seconds(*, since: datetime, until: datetime, overlay: str = "") -> tuple[float, ...]:
    ticket_ids = Task.objects.filter(created_at__gte=since, created_at__lt=until)
    if overlay:
        ticket_ids = ticket_ids.filter(ticket__overlay=overlay)
    rows = (
        Task.objects.filter(ticket_id__in=ticket_ids.values("ticket_id"))
        .annotate(last_end=Max("attempts__ended_at"))
        .order_by("ticket_id", "created_at", "pk")
        .values("ticket_id", "phase", "last_end", "admitted_at", "status")
    )
    gaps: list[float] = []
    previous_ticket_id: int | None = None
    previous_phase: str | None = None
    previous_end: datetime | None = None
    for row in rows.iterator():
        if previous_ticket_id == row["ticket_id"] and previous_phase != row["phase"]:
            start = row["admitted_at"] or (until if row["status"] == Task.Status.PENDING else None)
            if previous_end is not None and isinstance(start, datetime) and since <= previous_end <= start:
                gaps.append((start - previous_end).total_seconds())
        previous_ticket_id = row["ticket_id"]
        previous_phase = row["phase"]
        previous_end = row["last_end"] if isinstance(row["last_end"], datetime) else None
    return tuple(gaps)
