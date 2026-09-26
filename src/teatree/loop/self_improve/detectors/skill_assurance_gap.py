"""Bounded, owner-scoped incidents for missing or unverified headless skills."""

import re
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from itertools import starmap
from typing import ClassVar

from django.db.models import F, OuterRef, Subquery
from django.utils import timezone

from teatree.core.models import SelfImproveFiring, TaskAttempt, Ticket, TicketTransition
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport, DetectorScan

_WINDOW = timedelta(hours=6)
_REPAIR_AFTER = timedelta(hours=2)
_RECENT_ATTEMPTS = 200
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_./-]{0,79}$")
_ACTION_THRESHOLD = 3


@dataclass(slots=True)
class SkillAssuranceGapDetector:
    """Read only a bounded recent attempt tail; never mistake self-report for proof."""

    name: ClassVar[str] = "skill_assurance_gap"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "warn"
    max_rung: ClassVar[str] = ActionRung.TICKET
    auto_fix: ClassVar[bool] = False
    always_on: ClassVar[bool] = True

    overlay_name: str | None = None

    @property
    def dedup_prefix(self) -> str | None:
        return f"{self.name}::{self.overlay_name}::" if self.overlay_name else None

    def detect(self) -> list[DetectorReport]:
        return self.detect_checked().reports

    def detect_checked(self) -> DetectorScan:
        """Only a linked terminal repair ticket can close a prior skill gap."""
        terminal_times = self._terminal_ticket_times()
        reports, reopened = self._recent_gaps(terminal_times)
        return DetectorScan(
            reports,
            candidate_keys=frozenset(terminal_times),
            reopen_keys=frozenset(reopened),
        )

    def _terminal_ticket_times(self) -> dict[str, datetime]:
        terminal = Ticket.marker_release_states() | {Ticket.State.RETRO_RECORDED}
        terminal_edge = (
            TicketTransition.objects.filter(ticket_id=OuterRef("ticket_id"), to_state=OuterRef("ticket__state"))
            .exclude(from_state=F("to_state"))
            .order_by("-created_at", "-pk")
            .values("created_at")[:1]
        )
        rows = SelfImproveFiring.objects.filter(
            detector=self.name,
            resolved_at__isnull=True,
            ticket__state__in=terminal,
        ).annotate(terminal_at=Subquery(terminal_edge))
        if self.dedup_prefix:
            rows = rows.filter(dedup_key__startswith=self.dedup_prefix)
        return dict(rows.exclude(terminal_at__isnull=True).order_by("pk").values_list("dedup_key", "terminal_at")[:100])

    def _recent_gaps(self, terminal_times: dict[str, datetime]) -> tuple[list[DetectorReport], set[str]]:
        rows = TaskAttempt.objects.filter(started_at__gte=timezone.now() - _WINDOW)
        if self.overlay_name:
            rows = rows.filter(task__ticket__overlay=self.overlay_name)
        recent = rows.order_by("-pk").values("pk", "started_at", "task__ticket__overlay", "result__skill_assurance")[
            :_RECENT_ATTEMPTS
        ]
        grouped: dict[tuple[str, str, str], list[tuple[int, datetime]]] = defaultdict(list)
        for row in recent:
            receipt = row["result__skill_assurance"]
            if not isinstance(receipt, dict):
                continue
            if not receipt.get("requested"):
                continue  # No mandatory skill means no actionable application gap.
            status = receipt.get("status")
            if status not in {"missing", "injection_gap", "unverified"}:
                continue
            missing = receipt.get("missing")
            first_missing = missing[0] if isinstance(missing, list) and missing else None
            skill = (
                first_missing
                if isinstance(first_missing, str) and _SAFE_NAME.fullmatch(first_missing)
                else "application"
            )
            overlay = row["task__ticket__overlay"]
            grouped[overlay, status, skill].append((row["pk"], row["started_at"]))
        keys = set(starmap(self._key, grouped))
        closed = dict(
            SelfImproveFiring.objects.filter(
                detector=self.name, dedup_key__in=keys, resolved_at__isnull=False
            ).values_list("dedup_key", "resolved_at")
        )
        reports = []
        reopened = set()
        for (overlay, status, skill), attempts in sorted(grouped.items()):
            key = self._key(overlay, status, skill)
            cutoff = closed.get(key)
            terminal_at = terminal_times.get(key)
            eligible = [
                (pk, started_at)
                for pk, started_at in attempts
                if (cutoff is None or started_at > cutoff) and (terminal_at is None or started_at > terminal_at)
            ]
            if eligible:
                report = self._report(overlay, status, skill, eligible)
                if terminal_at is not None:
                    report = replace(report, max_rung=ActionRung.TICKET, requested_rung=ActionRung.TICKET)
                    reopened.add(key)
                reports.append(report)
        return reports, reopened

    def _key(self, overlay: str, status: str, skill: str) -> str:
        return canonical_key(self.name, f"{overlay}::{status}:{skill}")

    def _report(self, overlay: str, status: str, skill: str, attempts: list[tuple[int, datetime]]) -> DetectorReport:
        ids = [pk for pk, _started_at in attempts]
        cause = {
            "missing": "skill-missing",
            "injection_gap": "skill-delivery-gap",
            "unverified": "application-unverified",
        }[status]
        repeated = (
            len(ids) >= _ACTION_THRESHOLD
            or min(started_at for _pk, started_at in attempts) <= timezone.now() - _REPAIR_AFTER
        )
        rung = (
            ActionRung.TICKET
            if repeated
            else ActionRung.SLACK
            if status in {"missing", "injection_gap"}
            else ActionRung.STATUSLINE
        )
        identity = f"{overlay}::{status}:{skill}"
        return DetectorReport(
            detector=self.name,
            dedup_key=self._key(overlay, status, skill),
            state_hash=state_hash(identity, rung),
            severity="error" if status != "unverified" else self.severity,
            max_rung=rung,
            requested_rung=rung,
            summary=f"{len(ids)} recent headless attempt(s) had {cause} ({skill})",
            payload={
                "kind": "skill_assurance",
                "cause": cause,
                "count": len(ids),
                "ids": sorted(ids)[:5],
                "missing": [skill] if status == "missing" and skill != "application" else [],
                "overlay_name": overlay,
                "recovery_semantics": "manual_ticket_review",
                "requires_delivery": rung == ActionRung.SLACK,
                "suggested_action": "Inspect skill installation, the dispatch prompt, and the application receipt.",
            },
        )

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]
