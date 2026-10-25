"""Recent measured work durations used by the checking ETA."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from operator import itemgetter

from teatree.core.cycle_time.distribution import percentile
from teatree.core.cycle_time.eta import remaining_given_elapsed
from teatree.core.cycle_time.timeline import last_real_attempt_ends
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models.merge_clear import MergeAudit
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition

MIN_SAMPLES = 5
SOURCE = {
    "merge": "MergeClear.issued_at → MergeAudit.merged_at",
    "deploy": "MergeAudit.merged_at → first delivered transition",
}
TASK_SOURCE = "Task.admitted_at → last real TaskAttempt.ended_at"


@dataclass(frozen=True, slots=True)
class StepEstimate:
    step: str
    median_minutes: float
    p80_minutes: float
    n: int
    window: str
    source: str
    basis: str
    samples: tuple[float, ...]

    @classmethod
    def measured(cls, step: str, values: list[float], window: str) -> "StepEstimate":
        return cls(
            step=step,
            median_minutes=round(percentile(values, 0.5), 1),
            p80_minutes=round(percentile(values, 0.8), 1),
            n=len(values),
            window=window,
            source=SOURCE.get(step, TASK_SOURCE),
            basis="measured",
            samples=tuple(values),
        )

    def to_dict(self) -> dict[str, str | float | int]:
        return {
            "step": self.step,
            "median_minutes": self.median_minutes,
            "p80_minutes": self.p80_minutes,
            "n": self.n,
            "window": self.window,
            "source": self.source,
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class StepHistory:
    now: datetime
    spans: dict[str, list[tuple[datetime, float]]]

    @classmethod
    def measure(cls, now: datetime) -> "StepHistory":
        spans: dict[str, list[tuple[datetime, float]]] = {}
        cutoff = now - timedelta(days=7)
        task_ids = list(
            TaskAttempt.objects.filter(
                ended_at__gte=cutoff,
                ended_at__lte=now,
                task__status=Task.Status.COMPLETED,
                task__admitted_at__isnull=False,
            )
            .values_list("task_id", flat=True)
            .distinct()
        )
        last_ends = last_real_attempt_ends(task_ids)
        for task_id, admitted, phase in Task.objects.filter(pk__in=last_ends).values_list("pk", "admitted_at", "phase"):
            ended = last_ends[task_id]
            if admitted is not None and admitted <= ended <= now and ended >= cutoff:
                spans.setdefault(normalize_phase(phase), []).append((ended, (ended - admitted).total_seconds() / 60))

        audits = list(MergeAudit.objects.filter(merged_at__gte=cutoff, merged_at__lte=now).select_related("clear"))
        delivered: dict[int, list[datetime]] = {}
        for ticket_id, stamped in (
            TicketTransition.objects.filter(
                ticket_id__in=[audit.clear.ticket_id for audit in audits if audit.clear.ticket_id],
                to_state=Ticket.State.DELIVERED,
                created_at__gte=cutoff,
                created_at__lte=now,
            )
            .order_by("ticket_id", "created_at", "pk")
            .values_list("ticket_id", "created_at")
        ):
            delivered.setdefault(ticket_id, []).append(stamped)
        for audit in audits:
            if audit.merged_at >= audit.clear.issued_at:
                spans.setdefault("merge", []).append(
                    (audit.merged_at, (audit.merged_at - audit.clear.issued_at).total_seconds() / 60)
                )
            stamp = next((row for row in delivered.get(audit.clear.ticket_id, ()) if row >= audit.merged_at), None)
            if stamp is not None:
                spans.setdefault("deploy", []).append((stamp, (stamp - audit.merged_at).total_seconds() / 60))
        return cls(now=now, spans=spans)

    def estimate(self, step: str) -> StepEstimate | None:
        samples = self.spans.get(step, [])
        recent = [minutes for ended, minutes in samples if ended >= self.now - timedelta(hours=24)]
        older = [minutes for _ended, minutes in samples]
        if len(recent) >= MIN_SAMPLES:
            return StepEstimate.measured(step, recent, "24h")
        if len(older) >= MIN_SAMPLES:
            return StepEstimate.measured(step, older, "7d")
        return self._default_estimate(step)

    def remaining_measured(self, step: str, elapsed_minutes: float) -> tuple[float, float, str] | None:
        rows = self.spans.get(step, [])
        recent = [minutes for ended, minutes in rows if ended >= self.now - timedelta(hours=24)]
        all_samples = [minutes for _ended, minutes in rows]
        for window, samples in (("24h", recent), ("7d", all_samples)):
            if len(samples) >= MIN_SAMPLES:
                remaining = remaining_given_elapsed(samples, elapsed_minutes)
                if remaining is not None:
                    return *remaining, window
        return None

    def _default_estimate(self, step: str) -> StepEstimate | None:
        candidates = [
            (percentile(values, 0.5), percentile(values, 0.8), key, values, window)
            for key, values, window in self._candidate_sources()
        ]
        if not candidates:
            return None
        median_choice = max(candidates, key=itemgetter(0, 2))
        p80_choice = max(candidates, key=itemgetter(1, 2))
        median_step, p80_step = median_choice[2], p80_choice[2]
        basis = (
            f"default: slowest measured step ({median_step})"
            if median_step == p80_step
            else f"default: slowest measured steps (median {median_step}; p80 {p80_step})"
        )
        source = SOURCE.get(median_step, TASK_SOURCE)
        if p80_step != median_step:
            source = f"median {source}; p80 {SOURCE.get(p80_step, TASK_SOURCE)}"
        return StepEstimate(
            step=step,
            median_minutes=round(median_choice[0], 1),
            p80_minutes=round(p80_choice[1], 1),
            n=min(len(median_choice[3]), len(p80_choice[3])),
            window=median_choice[4] if median_choice[4] == p80_choice[4] else "24h+7d",
            source=source,
            basis=basis,
            samples=tuple(median_choice[3]),
        )

    def _candidate_sources(self) -> list[tuple[str, list[float], str]]:
        candidates: list[tuple[str, list[float], str]] = []
        for key, rows in self.spans.items():
            recent = [minutes for ended, minutes in rows if ended >= self.now - timedelta(hours=24)]
            older = [minutes for _ended, minutes in rows]
            values = recent if len(recent) >= MIN_SAMPLES else older
            if len(values) >= MIN_SAMPLES:
                candidates.append((key, values, "24h" if values is recent else "7d"))
        return candidates

    def remaining_default(self, elapsed_minutes: float) -> tuple[float, float, str] | None:
        survivors = [
            (key, conditional)
            for key, values, _window in self._candidate_sources()
            if (conditional := remaining_given_elapsed(values, elapsed_minutes)) is not None
        ]
        if not survivors:
            return None
        median_choice = max(survivors, key=lambda row: (row[1][0], row[0]))
        p80_choice = max(survivors, key=lambda row: (row[1][1], row[0]))
        median_step, p80_step = median_choice[0], p80_choice[0]
        basis = (
            f"default: slowest surviving step ({median_step})"
            if median_step == p80_step
            else f"default: slowest surviving steps (median {median_step}; p80 {p80_step})"
        )
        return median_choice[1][0], p80_choice[1][1], basis
