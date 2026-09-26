"""Reconcile OTel issue spans with the self-improve action ledger."""

import datetime as dt
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from django.db import OperationalError, ProgrammingError, transaction
from django.db.models import F

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.core.telemetry.admission import checked_factory_observations
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport, DetectorScan

_ACTION_GRACE = dt.timedelta(minutes=2)
_RECOVERY_BATCH = 100
_INCIDENT_ID = re.compile(r"[0-9a-f]{16}\Z")
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TelemetryActionGapDetector:
    """One fix ticket per observed factory issue whose action never landed."""

    name: ClassVar[str] = "telemetry_action_gap"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "error"
    max_rung: ClassVar[str] = ActionRung.TICKET
    auto_fix: ClassVar[bool] = False
    always_on: ClassVar[bool] = True

    directory: Path | None = None
    now: Callable[[], dt.datetime] = field(default=lambda: dt.datetime.now(tz=dt.UTC))

    def detect(self) -> list[DetectorReport]:
        return self.detect_checked().reports

    def detect_checked(self) -> DetectorScan:
        moment = self.now()
        cutoff = int((moment - _ACTION_GRACE).timestamp())
        telemetry = checked_factory_observations(directory=self.directory, now=moment)
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in telemetry.rows:
            if row["epoch"] <= cutoff and row["kind"] != self.name and row["severity"] != "info":
                grouped[row["incident_id"]].append(row)
        recovered_keys = self._actioned_gap_keys(moment)
        if not grouped:
            return DetectorScan([], complete=telemetry.complete, reason=telemetry.reason, candidate_keys=recovered_keys)
        try:
            actions = dict(
                SelfImproveFiring.objects.filter(dedup_key_digest__in=grouped).values_list(
                    "dedup_key_digest", "resolved_at"
                )
            )
        except (OperationalError, ProgrammingError) as exc:
            detail = str(exc).lower()
            missing_object = "dedup_key_digest" in detail or "teatree_self_improve_firing" in detail
            missing_schema = any(problem in detail for problem in ("no such column", "no such table", "does not exist"))
            if not (missing_object and missing_schema):
                raise
            logger.warning("self-improve firing digest schema unavailable; telemetry action gap deferred")
            return DetectorScan([], complete=False, reason="action_ledger_unavailable")
        reports = []
        for incident_id, rows in sorted(grouped.items()):
            if incident_id in actions and actions[incident_id] is None:
                continue
            resolved_at = actions.get(incident_id)
            missing_rows = (
                [row for row in rows if row["epoch"] > int(resolved_at.timestamp())]
                if resolved_at is not None
                else rows
            )
            if not missing_rows:
                continue
            first = missing_rows[0]
            reports.append(
                DetectorReport(
                    detector=self.name,
                    dedup_key=canonical_key(self.name, incident_id),
                    state_hash=state_hash(incident_id, "unactioned"),
                    severity=self.severity,
                    max_rung=self.max_rung,
                    requested_rung=ActionRung.TICKET,
                    summary=f"{len(missing_rows)} factory issue observations had no recorded action ({first['kind']})",
                    payload={
                        "kind": self.name,
                        "cause": "unactioned-issue",
                        "count": len(missing_rows),
                        "incident_id": incident_id,
                        "suggested_action": "Inspect the named detector and repair the failed action or delivery path.",
                    },
                )
            )
        return DetectorScan(
            reports,
            complete=telemetry.complete,
            reason=telemetry.reason,
            candidate_keys=recovered_keys,
        )

    def _actioned_gap_keys(self, moment: dt.datetime) -> frozenset[str]:
        """A later durable source action, not ledger absence, closes an old gap."""
        try:
            with transaction.atomic():
                gaps = list(
                    SelfImproveFiring.objects.select_for_update()
                    .filter(detector=self.name, resolved_at__isnull=True)
                    .order_by(F("payload__recovery_checked_at").asc(nulls_first=True), "pk")[:_RECOVERY_BATCH]
                )
                if not gaps:
                    return frozenset()
                by_incident: dict[str, tuple[str, dt.datetime]] = {}
                for gap in gaps:
                    incident_id = gap.dedup_key.removeprefix(f"{self.name}::")
                    if _INCIDENT_ID.fullmatch(incident_id):
                        by_incident[incident_id] = (gap.dedup_key, gap.first_fired_at)
                recovered: frozenset[str] = frozenset()
                if by_incident:
                    receipts = (
                        SelfImproveFiring.objects.filter(
                            dedup_key_digest__in=by_incident,
                            last_fired_at__lte=moment,
                        )
                        .exclude(detector=self.name)
                        .values_list("dedup_key_digest", "last_fired_at")
                    )
                    recovered = frozenset(
                        by_incident[digest][0] for digest, action_at in receipts if action_at > by_incident[digest][1]
                    )
                for gap in gaps:
                    gap.payload = {**gap.payload, "recovery_checked_at": moment.isoformat()}
                SelfImproveFiring.objects.bulk_update(gaps, ["payload"], batch_size=_RECOVERY_BATCH)
                return recovered
        except (OperationalError, ProgrammingError) as exc:
            detail = str(exc).lower()
            if "dedup_key_digest" in detail and any(
                marker in detail for marker in ("no such column", "does not exist")
            ):
                logger.warning("self-improve firing digest schema unavailable; gap recovery deferred")
                return frozenset()
            raise

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]
