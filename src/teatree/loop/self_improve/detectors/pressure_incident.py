"""Cluster repeated OTel admission denials by underlying pressure cause."""

import datetime as dt
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.core.telemetry.admission import checked_pressure_observations
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.resource_pressure import ResourceReading, measure_resources
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport, DetectorScan
from teatree.utils.ram_scope import cgroup_memory_probe_inert

_ACTION = {
    "load": "Inspect host and Docker load; stop optional heavy work until the governor resumes.",
    "memory": "Inspect box-scoped RAM and largest processes; reclaim only confirmed stale scratch.",
    "weekly-quota": "Inspect account reset times and defer new token-spending work.",
    "5h-quota": "Defer optional model calls until the short window resets.",
    "accounts-exhausted": "Check the account fleet and credential health before retrying.",
    "weekly-pace": "Reduce intake and inspect the last six hours of token yield.",
    "swap": "Inspect host swap and stop optional heavy work until swap falls below resume.",
    "yield-collapse": "Inspect failed task outcomes and repair the common failure before admitting more work.",
}
_FIRST_INCIDENT = 3
_ESCALATE_TO_SLACK = 10
_ESCALATE_TO_TICKET = 30


@dataclass(slots=True)
class PressureIncidentDetector:
    """One report per repeated cause, not one incident per admission span."""

    name: ClassVar[str] = "pressure_incident"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "warn"
    max_rung: ClassVar[str] = ActionRung.TICKET
    auto_fix: ClassVar[bool] = False
    always_on: ClassVar[bool] = True

    directory: Path | None = None
    now: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(tz=dt.UTC))
    read_resources: Callable[[], ResourceReading] = field(default=measure_resources)

    def detect(self) -> list[DetectorReport]:
        return self.detect_checked().reports

    def detect_checked(self) -> DetectorScan:
        moment = self.now()
        telemetry = checked_pressure_observations(directory=self.directory, now=moment)
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in telemetry.rows:
            if row.get("admit") is False or row.get("band") in {"shed", "halt"}:
                grouped[row["cause"]].append(row)
        reports: list[DetectorReport] = []
        for cause, rows in sorted(grouped.items()):
            count = len(rows)
            if count < _FIRST_INCIDENT:
                continue
            tier = (
                _ESCALATE_TO_TICKET
                if count >= _ESCALATE_TO_TICKET
                else _ESCALATE_TO_SLACK
                if count >= _ESCALATE_TO_SLACK
                else _FIRST_INCIDENT
            )
            peak = max(float(row["pressure"]) for row in rows)
            rung = (
                ActionRung.TICKET
                if tier == _ESCALATE_TO_TICKET
                else ActionRung.SLACK
                if tier == _ESCALATE_TO_SLACK
                else ActionRung.STATUSLINE
            )
            reports.append(
                DetectorReport(
                    detector=self.name,
                    dedup_key=canonical_key(self.name, cause),
                    state_hash=state_hash(cause, tier, moment.date().isoformat()),
                    severity=self.severity,
                    max_rung=rung,
                    requested_rung=rung,
                    summary=f"{count} pressure admissions in 30 min share cause {cause} (peak {peak:.2f})",
                    payload={
                        "cause": cause,
                        "observations": count,
                        "peak_pressure": peak,
                        "suggested_action": _ACTION.get(
                            cause, "Inspect the admission trace and its dominant pressure component."
                        ),
                    },
                    auto_fix=self.auto_fix,
                )
            )
        if cgroup_memory_probe_inert():
            reports.append(
                DetectorReport(
                    detector=self.name,
                    dedup_key=canonical_key(self.name, "cgroup-probe-inert"),
                    state_hash=state_hash("cgroup-probe-inert"),
                    severity="critical",
                    max_rung=ActionRung.SLACK,
                    requested_rung=ActionRung.SLACK,
                    summary="cgroup RAM floor is unreadable; the host RAM number may hide worker OOM risk",
                    payload={
                        "kind": "cgroup_probe_inert",
                        "cause": "cgroup-memory-unreadable",
                        "requires_delivery": True,
                        "suggested_action": "Repair cgroup memory path or permissions before trusting RAM admission.",
                    },
                )
            )
        reports.extend(self._resource_probe_reports())
        local_keys = frozenset(
            canonical_key(self.name, identity)
            for identity in ("cgroup-probe-inert", "ram-probe-inert", "disk-probe-inert")
        )
        return DetectorScan(
            reports,
            complete=telemetry.complete,
            reason=telemetry.reason,
            candidate_keys=local_keys | self._recovered_otel_keys(telemetry.rows, complete=telemetry.complete),
        )

    def _recovered_otel_keys(self, rows: list[dict], *, complete: bool) -> frozenset[str]:
        if not complete:
            return frozenset()
        latest: dict[str, tuple[int, bool]] = {}
        for row in rows:
            key = canonical_key(self.name, row["cause"])
            epoch = row["epoch"]
            healthy = row["admit"] is True and row["band"] == "full"
            previous = latest.get(key)
            if previous is None or epoch > previous[0]:
                latest[key] = (epoch, healthy)
            elif epoch == previous[0]:
                latest[key] = (epoch, previous[1] and healthy)
        full_epochs = {key: epoch for key, (epoch, healthy) in latest.items() if healthy}
        return frozenset(
            key
            for key, last_fired_at in SelfImproveFiring.objects.filter(
                detector=self.name,
                dedup_key__in=full_epochs,
                resolved_at__isnull=True,
            ).values_list("dedup_key", "last_fired_at")
            if full_epochs[key] > last_fired_at.timestamp()
        )

    def _resource_probe_reports(self) -> list[DetectorReport]:
        reading = self.read_resources()
        return [
            DetectorReport(
                detector=self.name,
                dedup_key=canonical_key(self.name, f"{resource}-probe-inert"),
                state_hash=state_hash(f"{resource}-probe-inert"),
                severity="critical",
                max_rung=ActionRung.SLACK,
                requested_rung=ActionRung.SLACK,
                summary=f"{resource} pressure probe is unreadable; its resource guard cannot protect the worker",
                payload={
                    "kind": f"{resource}_probe_inert",
                    "cause": f"{resource}-unreadable",
                    "requires_delivery": True,
                    "suggested_action": f"Restore the {resource} pressure probe before trusting resource admission.",
                },
            )
            for resource, inert in (("ram", reading.ram_probe_inert), ("disk", reading.disk_probe_inert))
            if inert
        ]

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]


__all__ = ["PressureIncidentDetector"]
