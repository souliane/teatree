"""Elapsed time from a ticket's first admission to its pull request merge."""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import F

from teatree.core.models.pull_request import PullRequest


@dataclass(frozen=True, slots=True)
class OpenPullRequest:
    url: str
    age_hours: float | None


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


def open_pull_requests(*, now: datetime, overlay: str = "") -> tuple[OpenPullRequest, ...]:
    rows = PullRequest.objects.live().with_flow_stamps()
    if overlay:
        rows = rows.filter(overlay=overlay)
    rows = rows.order_by(F("flow_started_at").asc(nulls_last=True), "pk")
    return tuple(
        OpenPullRequest(
            url=row.url,
            age_hours=max(0.0, (now - row.flow_started_at).total_seconds() / 3600)
            if row.flow_started_at is not None
            else None,
        )
        for row in rows.iterator()
    )
