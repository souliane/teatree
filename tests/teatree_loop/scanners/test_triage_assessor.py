"""DB-backed tests for ``TriageAssessorScanner``.

The scanner discovers OPEN ``needs-triage`` issues on the operator's host and
queues ONE headless ``triage_assessing`` task carrying a bounded serialized issue
list behind an ASK-GATE marker. It performs ZERO host writes (it never closes or
comments — that is the interactive approval skill's job) and self-gates on a
cadence (``Session.started_at`` last-run + in-flight-task dedup, the scanning_news
shape). Issues already carrying a ``PendingTriageRecommendation`` row are dropped.
Zero survivors ⇒ no task and a held signal.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import NEEDS_TRIAGE_LABEL, PendingTriageRecommendation, Ticket
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.loop.scanners.base import PartialScanError
from teatree.loop.scanners.needs_triage_query import needs_triage_issues
from teatree.loop.scanners.triage_assessor import TRIAGE_ASSESSOR_PHASE, TriageAssessorScanner
from teatree.types import RawAPIDict


@dataclass
class _Host:
    """Minimal CodeHostBackend stub — only the methods the scanner calls, plus a write spy."""

    user: str = "alice"
    issues: list[RawAPIDict] = field(default_factory=list)
    assigned_issues: list[RawAPIDict] | None = None
    writes: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_assigned_issues(self, *, assignee: str, repo_slugs: tuple[str, ...] = ()) -> list[RawAPIDict]:
        _ = (assignee, repo_slugs)
        return self.issues if self.assigned_issues is None else self.assigned_issues

    def list_labeled_issues(self, *, label: str, repo_slugs: tuple[str, ...]) -> list[RawAPIDict]:
        _ = (label, repo_slugs)
        return self.issues

    def close_issue(self, *args: object, **kwargs: object) -> None:
        self.writes.append("close_issue")

    def post_issue_comment(self, *args: object, **kwargs: object) -> None:
        self.writes.append("post_issue_comment")

    def update_issue(self, *args: object, **kwargs: object) -> None:
        self.writes.append("update_issue")


def _issue(url: str, *, title: str = "Do the thing", labels: list[str] | None = None) -> RawAPIDict:
    return {
        "web_url": url,
        "title": title,
        "state": "open",
        "labels": labels if labels is not None else [NEEDS_TRIAGE_LABEL],
    }


def _scanner(host: _Host, **kw: object) -> TriageAssessorScanner:
    return TriageAssessorScanner(host=host, overlay_name="acme", **kw)


def _last_task(overlay: str = "acme") -> Task | None:
    return Task.objects.filter(ticket__overlay=overlay, phase=TRIAGE_ASSESSOR_PHASE).order_by("-id").first()


def _backdate(task: Task, *, hours: int) -> None:
    Session.objects.filter(pk=task.session_id).update(started_at=timezone.now() - timedelta(hours=hours))


class TriageAssessorScanTests(TestCase):
    def setUp(self) -> None:
        dispatchable = patch.object(Ticket, "has_dispatchable_overlay", return_value=True)
        dispatchable.start()
        self.addCleanup(dispatchable.stop)

    def test_labeled_read_failure_without_partial_result_channel_raises(self) -> None:
        host = _Host(assigned_issues=[])
        with (
            patch.object(host, "list_labeled_issues", side_effect=RuntimeError("forge unavailable")),
            pytest.raises(RuntimeError, match="forge unavailable"),
        ):
            needs_triage_issues(host, ("alice",), repo_slugs=("souliane/teatree",), by_label=True)

    URL = "https://github.com/souliane/teatree/issues/700"
    URL_B = "https://github.com/souliane/teatree/issues/701"

    def test_bootstrap_queues_one_task_with_ask_gate_and_issue_list(self) -> None:
        host = _Host(issues=[_issue(self.URL, title="Broken login")])
        signals = _scanner(host).scan()

        assert len(signals) == 1
        signal = signals[0]
        assert signal.kind == "triage_assessor.queued"
        assert signal.payload["overlay"] == "acme"
        assert signal.payload["phase"] == TRIAGE_ASSESSOR_PHASE
        assert signal.payload["issue_count"] == 1
        assert signal.payload["trigger"] == "bootstrap"

        task = _last_task()
        assert task is not None
        assert task.phase == TRIAGE_ASSESSOR_PHASE
        assert task.status == Task.Status.PENDING
        assert "ASK-GATE" in task.execution_reason
        assert self.URL in task.execution_reason
        assert "Broken login" in task.execution_reason

    def test_scanner_performs_no_host_writes(self) -> None:
        host = _Host(issues=[_issue(self.URL), _issue(self.URL_B)])
        _scanner(host).scan()
        assert host.writes == []

    def test_no_needs_triage_issues_queues_nothing(self) -> None:
        host = _Host(issues=[_issue(self.URL, labels=["bug"])])
        signals = _scanner(host).scan()
        assert [signal.kind for signal in signals] == ["triage_assessor.held"]
        assert signals[0].payload["retry"] is False
        assert _last_task() is None

    def test_unassigned_owned_repo_issue_queues_assessment(self) -> None:
        host = _Host(issues=[_issue(self.URL)], assigned_issues=[])

        with patch.object(host, "list_assigned_issues", side_effect=RuntimeError("assignee lookup unavailable")):
            signals = _scanner(host, repo_slugs=("souliane/teatree",)).scan()

        assert [signal.kind for signal in signals] == ["triage_assessor.queued"]
        assert self.URL in _last_task().execution_reason

    def test_labeled_read_failure_keeps_assigned_fallback(self) -> None:
        host = _Host(assigned_issues=[_issue(self.URL)])
        with (
            patch.object(host, "list_labeled_issues", side_effect=RuntimeError("forge unavailable")),
            pytest.raises(PartialScanError) as failure,
        ):
            _scanner(host, repo_slugs=("souliane/teatree",)).scan()
        assert [signal.kind for signal in failure.value.signals] == ["triage_assessor.queued"]
        assert self.URL in _last_task().execution_reason

    def test_labeled_read_failure_with_no_assigned_issues_is_not_a_clean_hold(self) -> None:
        host = _Host(assigned_issues=[])
        with (
            patch.object(host, "list_labeled_issues", side_effect=RuntimeError("forge unavailable")),
            pytest.raises(PartialScanError) as failure,
        ):
            _scanner(host, repo_slugs=("souliane/teatree",)).scan()
        assert failure.value.signals == []
        assert _last_task() is None

    def test_assigned_read_failure_is_not_a_clean_hold(self) -> None:
        host = _Host(issues=[])
        with (
            patch.object(host, "list_assigned_issues", side_effect=RuntimeError("forge unavailable")),
            pytest.raises(PartialScanError) as failure,
        ):
            _scanner(host).scan()
        assert failure.value.signals == []

    def test_unassigned_issue_is_read_when_identity_is_unavailable(self) -> None:
        host = _Host(user="", issues=[_issue(self.URL)], assigned_issues=[])
        signals = _scanner(host, repo_slugs=("souliane/teatree",)).scan()
        assert [signal.kind for signal in signals] == ["triage_assessor.queued"]

    def test_explicit_empty_owned_repo_scope_queues_nothing(self) -> None:
        host = _Host(issues=[_issue(self.URL)])
        signals = _scanner(host, repo_slugs=()).scan()
        assert [signal.kind for signal in signals] == ["triage_assessor.held"]
        assert signals[0].payload["retry"] is False
        assert _last_task() is None

    def test_labeled_issue_assigned_to_someone_else_is_assessed(self) -> None:
        issue = _issue(self.URL)
        issue["assignee"] = {"login": "other"}
        host = _Host(issues=[issue], assigned_issues=[])

        signals = _scanner(host, repo_slugs=("souliane/teatree",)).scan()

        assert [signal.kind for signal in signals] == ["triage_assessor.queued"]
        assert self.URL in _last_task().execution_reason

    def test_closed_issue_is_ignored(self) -> None:
        issue = _issue(self.URL)
        issue["state"] = "closed"
        assert [signal.kind for signal in _scanner(_Host(issues=[issue])).scan()] == ["triage_assessor.held"]

    def test_no_identity_resolves_to_no_scan(self) -> None:
        assert [signal.kind for signal in _scanner(_Host(user="", issues=[_issue(self.URL)])).scan()] == [
            "triage_assessor.held"
        ]

    def test_issue_with_existing_recommendation_is_dropped(self) -> None:
        PendingTriageRecommendation.record_candidate(issue_url=self.URL, verdict="keep")
        host = _Host(issues=[_issue(self.URL)])
        assert [signal.kind for signal in _scanner(host).scan()] == ["triage_assessor.held"]
        assert _last_task() is None

    def test_only_survivors_are_queued(self) -> None:
        PendingTriageRecommendation.record_candidate(issue_url=self.URL, verdict="keep")
        host = _Host(issues=[_issue(self.URL), _issue(self.URL_B, title="Still open")])
        signals = _scanner(host).scan()
        assert len(signals) == 1
        assert signals[0].payload["issue_count"] == 1
        task = _last_task()
        assert task is not None
        assert self.URL_B in task.execution_reason
        assert self.URL not in task.execution_reason

    def test_max_issues_per_tick_truncates_the_serialized_list(self) -> None:
        host = _Host(issues=[_issue(self.URL), _issue(self.URL_B)])
        signals = _scanner(host, max_issues_per_tick=1).scan()
        assert len(signals) == 1
        assert signals[0].payload["issue_count"] == 1

    def test_in_flight_task_blocks_new_queueing(self) -> None:
        host = _Host(issues=[_issue(self.URL)])
        first = _scanner(host).scan()
        assert len(first) == 1
        prior = _last_task()
        assert prior is not None
        # Leave PENDING and backdate so cadence WOULD trigger — the in-flight lock holds.
        _backdate(prior, hours=48)
        assert [signal.kind for signal in _scanner(host).scan()] == ["triage_assessor.not_due"]
        assert _last_task().pk == prior.pk

    def test_cadence_elapsed_queues_new_task(self) -> None:
        host = _Host(issues=[_issue(self.URL)])
        _scanner(host).scan()
        prior = _last_task()
        assert prior is not None
        Task.objects.filter(pk=prior.pk).update(status=Task.Status.COMPLETED)
        _backdate(prior, hours=25)
        # A survivor still exists (no recommendation recorded yet), so cadence re-queues.
        second = _scanner(host).scan()
        assert len(second) == 1
        assert second[0].payload["trigger"] == "cadence"
        assert _last_task().pk != prior.pk

    def test_identities_override_current_user(self) -> None:
        host = _Host(user="", issues=[_issue(self.URL)])
        signals = _scanner(host, identities=("bob",)).scan()
        assert len(signals) == 1
