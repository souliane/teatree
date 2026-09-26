"""Find failed work and unanswered messages from the factory's durable ledgers."""

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

from django.db.models import Q
from django.utils import timezone

from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import DeferredQuestion, PendingChatInjection, SelfImproveFiring, Task, Ticket
from teatree.core.telemetry.admission import checked_lifecycle_observations
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.self_improve.dedup import canonical_key, state_hash
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport, DetectorScan

_MESSAGE_WAIT = timedelta(hours=1)
_POST_WAIT = timedelta(minutes=10)
_CLAIM_WAIT = timedelta(minutes=10)
_RECURRING_FAILURE = 3
_OWNER_ALERT_WAIT = timedelta(minutes=30)
_REPAIR_WAIT = timedelta(hours=2)
_RECOVERED_KINDS = {FailureKind.CANCELLED, FailureKind.SUPERSEDED}
_SUBSTANTIVE_REPLIES = {
    PendingChatInjection.AnswerKind.SIMPLE,
    PendingChatInjection.AnswerKind.QUESTION_REPLY,
}


@dataclass(slots=True)
class LifecycleIncidentDetector:
    name: ClassVar[str] = "lifecycle_incident"
    tier: ClassVar[str] = "cheap"
    severity: ClassVar[str] = "warn"
    max_rung: ClassVar[str] = ActionRung.TICKET
    auto_fix: ClassVar[bool] = False
    always_on: ClassVar[bool] = True

    directory: Path | None = None
    now: Callable[[], datetime] = timezone.now
    overlay_name: str | None = None
    owner_alert_after: timedelta = _OWNER_ALERT_WAIT
    repair_after: timedelta = _REPAIR_WAIT

    @property
    def dedup_prefix(self) -> str | None:
        return f"{self.name}::{self.overlay_name}::" if self.overlay_name else None

    def _key(self, kind: str, cause: str) -> str:
        identity = f"{kind}:{cause}"
        return canonical_key(self.name, f"{self.overlay_name}::{identity}" if self.overlay_name else identity)

    def _lifecycle_rung(
        self,
        *,
        kind: str,
        cause: str,
        ids: list[int],
        now: datetime,
        ticket_allowed: bool = True,
    ) -> str:
        if ticket_allowed and len(ids) >= _RECURRING_FAILURE:
            return ActionRung.TICKET
        firing = SelfImproveFiring.objects.filter(detector=self.name, dedup_key=self._key(kind, cause)).first()
        if firing is None or firing.resolved_at is not None:
            return ActionRung.STATUSLINE
        if ticket_allowed and firing.last_action == ActionRung.TICKET:
            return ActionRung.TICKET
        age = now - firing.first_fired_at
        if ticket_allowed and age >= self.repair_after and firing.last_action == ActionRung.SLACK:
            return ActionRung.TICKET
        if age >= self.owner_alert_after:
            return ActionRung.SLACK
        return ActionRung.STATUSLINE

    def detect(self) -> list[DetectorReport]:
        return self.detect_checked().reports

    def detect_checked(self) -> DetectorScan:
        """Historical attempt bursts stay open until manual review closes their firing.

        The bounded span records are not a durable inventory of all failed tasks;
        their disappearance cannot prove repair or justify automatic resolution.
        """
        now = self.now()
        telemetry = checked_lifecycle_observations(directory=self.directory, now=now)
        reports = self._failed_tasks(now) + self._recent_attempt_failures(telemetry.rows) + self._stalled_tasks(now)
        reports.extend(self._inbound_questions(now))
        reports.extend(self._outbound_questions(now))
        prefix = self._key("attempt_failure_burst", "")
        return DetectorScan(
            reports,
            complete=telemetry.complete,
            reason=telemetry.reason,
            protected_prefixes=(prefix,),
        )

    def _recent_attempt_failures(self, observations: list[dict]) -> list[DetectorReport]:
        """OTel catches repeated failed attempts even after their tasks recover."""
        attempts = [
            row
            for row in observations
            if row["kind"] == "attempt.finished" and row["cause"] not in {"success", *_RECOVERED_KINDS}
        ]
        cutoffs = self._attempt_evidence_cutoffs(attempts)
        by_cause: dict[str, set[int]] = defaultdict(set)
        for row in attempts:
            cursor = cutoffs.get(self._key("attempt_failure_burst", row["cause"]))
            if cursor is None or row["epoch"] > cursor:
                by_cause[row["cause"]].add(row["task_id"])
        if by_cause:
            candidate_ids = {task_id for ids in by_cause.values() for task_id in ids}
            repair_ids = set(
                Task.objects.filter(pk__in=candidate_ids, ticket__extra__source="self_improve").values_list(
                    "pk", flat=True
                )
            )
            eligible_ids = candidate_ids - repair_ids
            if self.overlay_name:
                eligible_ids = set(
                    Task.objects.filter(pk__in=eligible_ids, ticket__overlay=self.overlay_name).values_list(
                        "pk", flat=True
                    )
                )
            by_cause = {cause: ids & eligible_ids for cause, ids in by_cause.items()}
        reports: list[DetectorReport] = []
        for cause, ids in sorted(by_cause.items()):
            if len(ids) < _RECURRING_FAILURE:
                continue
            report = self._report(
                kind="attempt_failure_burst",
                cause=cause,
                ids=sorted(ids),
                rung=ActionRung.TICKET,
                action="Inspect recent failed attempts, including tasks that were reopened or retried.",
            )
            cursor = cutoffs.get(report.dedup_key)
            if cursor is not None:
                report.payload["attempt_after_epoch"] = cursor
            reports.append(report)
        return reports

    def _attempt_evidence_cutoffs(self, attempts: list[dict]) -> dict[str, float]:
        keys = {self._key("attempt_failure_burst", row["cause"]) for row in attempts}
        cutoffs: dict[str, float] = {}
        for key, resolved_at, payload in SelfImproveFiring.objects.filter(
            detector=self.name, dedup_key__in=keys
        ).values_list("dedup_key", "resolved_at", "payload"):
            if resolved_at is not None:
                cutoffs[key] = resolved_at.timestamp()
            elif isinstance(payload, dict):
                prior = payload.get("attempt_after_epoch")
                if isinstance(prior, (int, float)) and not isinstance(prior, bool) and math.isfinite(prior):
                    cutoffs[key] = float(prior)
        return cutoffs

    def _report(
        self,
        *,
        kind: str,
        cause: str,
        ids: list[int],
        rung: str,
        action: str,
    ) -> DetectorReport:
        count = len(ids)
        return DetectorReport(
            detector=self.name,
            dedup_key=self._key(kind, cause),
            state_hash=state_hash(kind, cause, tuple(sorted(ids)), rung),
            severity="error" if rung == ActionRung.TICKET else self.severity,
            max_rung=rung,
            requested_rung=rung,
            summary=f"{count} {kind.replace('_', ' ')} ({cause})",
            payload={
                "kind": kind,
                "cause": cause,
                "count": count,
                "ids": sorted(ids)[:5],
                "suggested_action": action,
                **({"recovery_semantics": "manual_ticket_review"} if kind == "attempt_failure_burst" else {}),
                **(
                    {"overlay_name": self.overlay_name}
                    if self.overlay_name and not kind.startswith("outbound_")
                    else {}
                ),
                **({"requires_delivery": True} if rung == ActionRung.SLACK else {}),
            },
        )

    def _failed_tasks(self, now: datetime) -> list[DetectorReport]:
        by_cause: dict[str, list[int]] = defaultdict(list)
        repair_by_cause: dict[str, list[int]] = defaultdict(list)
        settled = Ticket.marker_release_states() | {Ticket.State.RETROSPECTED}
        tasks = (
            Task.objects.filter(status=Task.Status.FAILED)
            .exclude(failure_kind__in=_RECOVERED_KINDS)
            .exclude(ticket__state__in=settled)
        )
        if self.overlay_name:
            tasks = tasks.filter(ticket__overlay=self.overlay_name)
        for task_id, failure_kind, source in tasks.values_list("pk", "failure_kind", "ticket__extra__source"):
            groups = repair_by_cause if source == "self_improve" else by_cause
            groups[failure_kind or FailureKind.UNRECORDED].append(task_id)
        reports = [
            self._report(
                kind="task_failed",
                cause=cause,
                ids=ids,
                rung=self._lifecycle_rung(kind="task_failed", cause=cause, ids=ids, now=now),
                action="Inspect the named failure kind and repair the common cause before re-dispatch.",
            )
            for cause, ids in sorted(by_cause.items())
        ]
        reports.extend(
            self._report(
                kind="repair_task_failed",
                cause=cause,
                ids=ids,
                rung=self._lifecycle_rung(
                    kind="repair_task_failed", cause=cause, ids=ids, now=now, ticket_allowed=False
                ),
                action="Inspect the existing self-improve repair ticket; do not create a second repair ticket.",
            )
            for cause, ids in sorted(repair_by_cause.items())
        )
        return reports

    def _stalled_tasks(self, now: datetime) -> list[DetectorReport]:
        stalled = Task.objects.filter(
            status=Task.Status.CLAIMED,
            lease_expires_at__lt=now - _CLAIM_WAIT,
        ).filter(heartbeat_at__isnull=True) | Task.objects.filter(
            status=Task.Status.CLAIMED,
            lease_expires_at__lt=now - _CLAIM_WAIT,
            heartbeat_at__lt=now - _CLAIM_WAIT,
        )
        if self.overlay_name:
            stalled = stalled.filter(ticket__overlay=self.overlay_name)
        normal_ids: list[int] = []
        repair_ids: list[int] = []
        for task_id, source in stalled.values_list("pk", "ticket__extra__source")[:100]:
            (repair_ids if source == "self_improve" else normal_ids).append(task_id)
        if not normal_ids and not repair_ids:
            return []
        reports = []
        if normal_ids:
            reports.append(
                self._report(
                    kind="task_stalled",
                    cause="expired_lease_no_heartbeat",
                    ids=normal_ids,
                    rung=ActionRung.TICKET,
                    action="Check worker liveness and the claim-recovery sweep before re-dispatch.",
                )
            )
        if repair_ids:
            reports.append(
                self._report(
                    kind="repair_task_stalled",
                    cause="expired_lease_no_heartbeat",
                    ids=repair_ids,
                    rung=self._lifecycle_rung(
                        kind="repair_task_stalled",
                        cause="expired_lease_no_heartbeat",
                        ids=repair_ids,
                        now=now,
                        ticket_allowed=False,
                    ),
                    action="Inspect the existing self-improve repair ticket and its stalled task.",
                )
            )
        return reports

    def _inbound_questions(self, now: datetime) -> list[DetectorReport]:
        rows = PendingChatInjection.objects.filter(
            received_at__lt=now - _MESSAGE_WAIT,
            answered_at__isnull=True,
            loop_response_confirmed_at__isnull=True,
        )
        if self.overlay_name:
            rows = rows.filter(overlay=self.overlay_name)
        rows = rows.filter(
            Q(loop_replied_at__isnull=True)
            | Q(answer_kind__in=_SUBSTANTIVE_REPLIES)
            | Q(text__regex=PendingChatInjection.question_text_regex)
        )
        ids = [
            row.pk
            for row in rows.only("id", "text", "answer_kind", "loop_replied_at")[:100]
            if row.is_question or row.loop_replied_at is None or row.answer_kind in _SUBSTANTIVE_REPLIES
        ]
        if not ids:
            return []
        return [
            self._report(
                kind="inbound_unanswered",
                cause="message_without_confirmed_response",
                ids=ids,
                rung=self._lifecycle_rung(
                    kind="inbound_unanswered", cause="message_without_confirmed_response", ids=ids, now=now
                ),
                action="Inspect the inbound answer loop and verify a confirmed response in the original thread.",
            )
        ]

    def _outbound_questions(self, now: datetime) -> list[DetectorReport]:
        pending = DeferredQuestion.pending().filter(audience=DeferredQuestion.Audience.OWNER_QUESTION)
        unposted = list(pending.filter(slack_ts="", created_at__lt=now - _POST_WAIT).values_list("pk", flat=True)[:100])
        unanswered = list(
            pending.exclude(slack_ts="").filter(created_at__lt=now - _MESSAGE_WAIT).values_list("pk", flat=True)[:100]
        )
        reports = []
        if unposted:
            # Unattributed questions can alert the owner but cannot assign a scoped repair ticket safely.
            reports.append(
                self._report(
                    kind="outbound_unposted",
                    cause="question_delivery_gap",
                    ids=unposted,
                    rung=ActionRung.SLACK if self.overlay_name else ActionRung.TICKET,
                    action="Repair the question poster or its Slack transport; keep the durable question pending.",
                )
            )
        if unanswered:
            reports.append(
                self._report(
                    kind="outbound_unanswered",
                    cause="awaiting_user_reply",
                    ids=unanswered,
                    rung=ActionRung.STATUSLINE,
                    action="Keep the question visible in its original thread; do not infer consent from silence.",
                )
            )
        return reports

    def scan(self) -> list[ScanSignal]:
        return [report.to_signal() for report in self.detect()]
