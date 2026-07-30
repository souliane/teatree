"""Periodic needs-triage assessor scanner — discover OPEN ``needs-triage`` issues.

Companion to the ``t3:triage-assessor`` shell-denied agent and the interactive
``t3:triaging-issues`` skill. Each tick lists the operator's OPEN ``needs-triage``
issues (via the shared :func:`teatree.loop.scanners.needs_triage_query.needs_triage_issues`),
drops any already carrying a :class:`~teatree.core.models.pending_triage_recommendation.PendingTriageRecommendation`
row, and — behind a cadence + in-flight-task lock — queues ONE headless
``triage_assessing`` task carrying a bounded serialized issue list behind an
ASK-GATE marker.

Two hard invariants (mirroring the disposition scanner's conservative doctrine):

* **Zero host writes.** This scanner NEVER closes, comments on, or relabels an
    issue — it only reads and queues an assessment. Acting is the interactive
    approval skill's job (``gh`` on user approval), gated by the ask-gate rows the
    recorder persists from the agent's returned envelope.
* **Nothing acts autonomously.** The queued task routes to a shell-denied agent
    that RETURNS a typed ``triage_recommendations`` envelope; the recorder persists
    one PENDING ask-gate row per issue plus one ``DeferredQuestion`` DMing the user.

The dedup / last-run / bootstrap-cadence machinery is shared with the other
periodic task-queuing scanners via
:class:`teatree.loop.scanners.phase_cadence.PhaseCadence`; the survivors filter (a
genuine variant — no other scanner reads the forge) stays local. The whole
scanner is gated default-OFF one layer up
(:func:`teatree.loop.scanner_factories._triage_assessor_scanner_for`): with
``triage_assessor_enabled = false`` no scanner is built, so this never runs.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.utils import timezone

from teatree.core.backend_protocols import CodeHostBackend
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.needs_triage_query import _issue_labels, _issue_title, _issue_url, needs_triage_issues
from teatree.loop.scanners.phase_cadence import PhaseCadence
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.core.models import PendingTriageRecommendation as _PendingTriageRecommendation

logger = logging.getLogger(__name__)

#: Canonical phase token written to ``Task.phase`` for triage-assessment tasks.
TRIAGE_ASSESSOR_PHASE = "triage_assessing"


@dataclass(slots=True)
class TriageAssessorScanner:
    """Queue a periodic ``triage_assessing`` task for OPEN ``needs-triage`` issues.

    Configuration is passed explicitly (rather than read from a global at scan
    time) so test setup is deterministic and the wiring layer
    (:func:`teatree.loop.scanner_factories._triage_assessor_scanner_for`) is the
    single place that resolves settings to kwargs. The on/off decision lives at
    the wiring layer (``triage_assessor_enabled``); the scanner itself always
    scans when invoked, but emits nothing when there are no survivors or the
    cadence/in-flight lock holds.
    """

    host: CodeHostBackend
    overlay_name: str = ""
    identities: tuple[str, ...] = field(default_factory=tuple)
    cadence_hours: int = 24
    max_issues_per_tick: int = 10
    name: str = "triage_assessor"

    def scan(self) -> list[ScanSignal]:
        assignees = self._resolve_identities()
        if not assignees:
            return []
        cadence = PhaseCadence(self.overlay_name, phase=TRIAGE_ASSESSOR_PHASE, cadence_hours=self.cadence_hours)
        if cadence.in_flight_exists():
            return []

        trigger = cadence.evaluate_trigger(now=timezone.now(), last_run_at=cadence.last_run_at())
        if trigger is None:
            return []

        survivors = self._survivors(assignees)
        if not survivors:
            return []

        task = cadence.queue_task(
            placeholder_issue_url=f"triage-assessor://{self.overlay_name}",
            agent_id=f"triage-assessor-{self.overlay_name}",
            execution_reason=self._execution_reason(survivors=survivors, trigger=trigger),
            subject=f"Triage assessment: {self.overlay_name}",
            log_label="TriageAssessorScanner",
        )
        if task is None:
            return []
        return [
            ScanSignal(
                kind="triage_assessor.queued",
                summary=f"triage-assessor queued for {self.overlay_name} ({len(survivors)} issue(s), {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "phase": TRIAGE_ASSESSOR_PHASE,
                    "task_id": task.pk,
                    "issue_count": len(survivors),
                    "trigger": trigger,
                },
            ),
        ]

    def _resolve_identities(self) -> tuple[str, ...]:
        if self.identities:
            return tuple(dict.fromkeys(self.identities))
        user = self.host.current_user()
        return (user,) if user else ()

    def _survivors(self, assignees: tuple[str, ...]) -> list[RawAPIDict]:
        """OPEN ``needs-triage`` issues with no existing recommendation, truncated per tick.

        An issue already carrying a :class:`PendingTriageRecommendation` row (in
        any state) is dropped — it has already been assessed and awaits the user's
        decision, so re-queuing it would double-ask. The remainder is bounded to
        ``max_issues_per_tick`` so a queued task's serialized list stays reviewable.
        """
        recommendation_model = _recommendation_model()
        survivors: list[RawAPIDict] = []
        for issue in needs_triage_issues(self.host, assignees):
            url = _issue_url(issue)
            if not url:
                continue
            if (
                recommendation_model is not None
                and recommendation_model.objects.filter(url_hash=recommendation_model.hash_url(url)).exists()
            ):
                continue
            survivors.append(issue)
            if len(survivors) >= self.max_issues_per_tick:
                break
        return survivors

    @staticmethod
    def _execution_reason(*, survivors: list[RawAPIDict], trigger: str) -> str:
        """Build the dispatcher directive: the ASK-GATE marker + the serialized issue list.

        The ASK-GATE substring is load-bearing — it is the channel the dispatched
        agent reads to know it must RETURN a ``triage_recommendations`` envelope and
        NEVER act. Each ISSUE line is ``<url> | <title> | <labels>`` so the agent has
        the bounded work list embedded (souliane/teatree is public — it WebFetches
        each issue page for the full body).
        """
        lines = "\n".join(
            f"- {_issue_url(issue)} | {_issue_title(issue)} | {','.join(_issue_labels(issue))}" for issue in survivors
        )
        return (
            f"Periodic needs-triage assessment ({trigger}) | ASK-GATE: do NOT act on any issue — "
            f"assess each ISSUE below (WebFetch the public issue page) and RETURN one "
            f"triage_recommendations item per issue (verdict keep/close/needs_info + rationale). "
            f"The recorder persists each as a PendingTriageRecommendation for user approval; "
            f"nothing is closed/commented without per-item approval.\nISSUES:\n{lines}"
        )


def _recommendation_model() -> "type[_PendingTriageRecommendation] | None":
    try:
        return cast("type[_PendingTriageRecommendation]", apps.get_model("core", "PendingTriageRecommendation"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no dedup
        return None


__all__ = [
    "TRIAGE_ASSESSOR_PHASE",
    "TriageAssessorScanner",
]
