"""Bounded local OTel observations joined to durable self-improvement actions."""

import datetime as dt
import logging
from dataclasses import dataclass
from operator import itemgetter

from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.core.selectors._helpers import _humanize_duration
from teatree.core.telemetry.admission import (
    checked_factory_observations,
    checked_lifecycle_observations,
    checked_pressure_observations,
)
from teatree.core.telemetry.observation_read import ObservationRead

logger = logging.getLogger(__name__)

OBSERVATION_WINDOW = dt.timedelta(days=1)
FEED_STALE_AFTER = dt.timedelta(minutes=10)
INCIDENT_ROWS = 12
INCIDENT_JOIN_BATCH = 100


@dataclass(frozen=True, slots=True)
class IncidentRow:
    kind: str
    cause: str
    severity: str
    count: int
    observed_for: str
    last_seen: dt.datetime
    last_action: str
    action_at: dt.datetime | None
    action_ticket_id: int | None


@dataclass(frozen=True, slots=True)
class TelemetrySource:
    name: str
    status: str
    last_seen_age: str
    last_seen: dt.datetime | None


@dataclass(frozen=True, slots=True)
class TelemetryView:
    incidents: tuple[IncidentRow, ...]
    sources: tuple[TelemetrySource, ...]
    error: str = ""

    @classmethod
    def degraded(cls) -> "TelemetryView":
        return cls((), (), "Telemetry reader unavailable")


def build_telemetry_view(*, now: dt.datetime | None = None) -> TelemetryView:
    """Read fixed-size JSONL tails and join only the incident digests in that tail."""
    moment = now or timezone.now()
    try:
        factory = checked_factory_observations(now=moment, window=OBSERVATION_WINDOW)
        pressure = checked_pressure_observations(now=moment, window=OBSERVATION_WINDOW)
        lifecycle = checked_lifecycle_observations(now=moment, window=OBSERVATION_WINDOW)
        return TelemetryView(
            incidents=_active_incidents(factory.rows, moment),
            sources=(
                _source("Factory events", factory, moment),
                _source("Pressure events", pressure, moment),
                _source("Lifecycle events", lifecycle, moment),
            ),
        )
    except Exception:
        logger.warning("dashboard telemetry read failed", exc_info=True)
        return TelemetryView.degraded()


def _source(name: str, observation: ObservationRead, now: dt.datetime) -> TelemetrySource:
    latest_epoch = max((row["epoch"] for row in observation.rows), default=None)
    if latest_epoch is None:
        return TelemetrySource(name, "unobserved" if observation.complete else "degraded", "", None)
    last_seen = dt.datetime.fromtimestamp(latest_epoch, tz=dt.UTC)
    age = now - last_seen
    status = "degraded" if not observation.complete else "stale" if age > FEED_STALE_AFTER else "current"
    return TelemetrySource(name, status, _humanize_duration(age.total_seconds()), last_seen)


def _active_incidents(rows: list[dict], now: dt.datetime) -> tuple[IncidentRow, ...]:
    ready_epoch = max(
        (row["epoch"] for row in rows if row["kind"] == "boot" and row["cause"] == "ready"),
        default=0,
    )
    latest: dict[str, dict] = {}
    for row in rows:
        if row["severity"] == "info" or (row["kind"] == "boot" and row["epoch"] <= ready_epoch):
            continue
        incident_id = row["incident_id"]
        if incident_id not in latest or row["epoch"] > latest[incident_id]["epoch"]:
            latest[incident_id] = row
    observed = sorted(latest.values(), key=itemgetter("epoch"), reverse=True)
    if not observed:
        return ()
    result = []
    for offset in range(0, len(observed), INCIDENT_JOIN_BATCH):
        batch = observed[offset : offset + INCIDENT_JOIN_BATCH]
        try:
            firings = {
                firing.dedup_key_digest: firing
                for firing in SelfImproveFiring.objects.filter(
                    dedup_key_digest__in=[row["incident_id"] for row in batch]
                ).order_by("last_fired_at")
            }
        except (OperationalError, ProgrammingError):
            firings = {}
        for row in batch:
            incident = _incident_from_observation(row, now, firings.get(row["incident_id"]))
            if incident is None:
                continue
            result.append(incident)
            if len(result) == INCIDENT_ROWS:
                return tuple(result)
    return tuple(result)


def _incident_from_observation(row: dict, now: dt.datetime, firing: SelfImproveFiring | None) -> IncidentRow | None:
    seen = dt.datetime.fromtimestamp(row["epoch"], tz=dt.UTC)
    if firing is not None and firing.resolved_at is not None:
        if firing.resolved_at >= seen:
            return None
        firing = None  # A reopened observation has no action in its new generation yet.
    first_seen = min(seen, firing.first_fired_at) if firing is not None else seen
    return IncidentRow(
        kind=row["kind"],
        cause=row["cause"],
        severity=row["severity"],
        count=row["count"],
        observed_for=_humanize_duration((now - first_seen).total_seconds()),
        last_seen=seen,
        last_action=firing.last_action if firing is not None else "none recorded",
        action_at=firing.last_fired_at if firing is not None else None,
        action_ticket_id=firing.ticket_id if firing is not None else None,  # ty: ignore[unresolved-attribute]
    )
