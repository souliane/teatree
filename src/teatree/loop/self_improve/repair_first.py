"""Repair-first firing markers and the delayed owner notification sweep."""

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.self_improve.detectors.base import DetectorReport

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OwnerAlertDelivery:
    sent: bool
    pulled: bool = False


@dataclass(frozen=True, slots=True)
class RepairMarker:
    began_at: dt.datetime
    summary: str
    notified_at: dt.datetime | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RepairMarker | None":
        raw = payload.get("repair_first")
        if not isinstance(raw, dict) or not isinstance(raw.get("began_at"), str):
            return None
        try:
            began_at = dt.datetime.fromisoformat(raw["began_at"])
            notified_at = dt.datetime.fromisoformat(raw["notified_at"]) if raw.get("notified_at") else None
        except (TypeError, ValueError):
            return None
        if not timezone.is_aware(began_at) or (notified_at is not None and not timezone.is_aware(notified_at)):
            return None
        summary = raw.get("summary")
        return cls(began_at, summary, notified_at) if isinstance(summary, str) and summary else None

    def as_payload(self) -> dict[str, str | None]:
        return {
            "began_at": self.began_at.isoformat(),
            "summary": self.summary,
            "notified_at": self.notified_at.isoformat() if self.notified_at else None,
        }


def repair_kind(report: DetectorReport) -> bool:
    return str(report.payload.get("kind", "")).startswith("repair_")


def probe_inert(report: DetectorReport) -> bool:
    return str(report.payload.get("kind", "")).endswith("_probe_inert")


def marked_report(report: DetectorReport, existing: SelfImproveFiring | None) -> DetectorReport:
    marker = RepairMarker.from_payload(existing.payload) if existing and existing.resolved_at is None else None
    marker = marker or RepairMarker(timezone.now(), report.summary)
    return replace(report, payload={**report.payload, "repair_first": marker.as_payload()})


def carry_open_marker(report: DetectorReport, existing: SelfImproveFiring | None) -> DetectorReport:
    marker = RepairMarker.from_payload(existing.payload) if existing and existing.resolved_at is None else None
    return replace(report, payload={**report.payload, "repair_first": marker.as_payload()}) if marker else report


@dataclass(frozen=True, slots=True)
class OwnerWaitChannel:
    deliver: Callable[[str, str], OwnerAlertDelivery]
    quiet_now: Callable[[], bool]
    wait: dt.timedelta


def wait_message(firing: SelfImproveFiring, marker: RepairMarker, wait: dt.timedelta) -> str:
    minutes = int(wait.total_seconds() // 60)
    lines = [f"[self-improve] Still open after {minutes} minutes: {marker.summary}"]
    if firing.ticket is not None:
        ticket = firing.ticket
        phases = [f"{task.phase} {task.status}" for task in ticket.tasks.order_by("pk")]
        lines.extend(
            (
                f"Repair: ticket {ticket.pk} ({ticket.short_description})",
                f"Tried: {', '.join(phases) if phases else 'no task yet'}",
            )
        )
    elif str(firing.payload.get("kind", "")).startswith("repair_"):
        lines.extend(("Repair: no second ticket for a repair incident", "Tried: repair-of-repair deferred"))
    else:
        lines.extend(("Repair: ticket deferred", "Tried: ticket creation pending"))
    return "\n".join(lines)


def sweep_owner_wait(
    reported: set[tuple[str, str]], channel: OwnerWaitChannel | None, *, now: dt.datetime
) -> list[int]:
    if channel is None or channel.quiet_now():
        return []
    notified: list[int] = []
    for firing in SelfImproveFiring.objects.filter(resolved_at__isnull=True).select_related("ticket"):
        marker = RepairMarker.from_payload(firing.payload)
        if marker is None or marker.notified_at is not None or now - marker.began_at < channel.wait:
            continue
        if (firing.detector, firing.dedup_key) not in reported:
            continue
        key = f"self-improve:still-open:{firing.pk}:{int(marker.began_at.timestamp())}"
        try:
            outcome = channel.deliver(wait_message(firing, marker, channel.wait), key)
        except Exception:
            logger.exception("self-improve owner wait delivery failed for firing %s", firing.pk)
            continue
        if not outcome.sent and not outcome.pulled:
            continue
        firing.payload = {
            **firing.payload,
            "repair_first": replace(marker, notified_at=now).as_payload(),
        }
        firing.save(update_fields=["payload"])
        notified.append(firing.pk)
    return notified
