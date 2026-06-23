"""DB-backed tests for ``TaskSweepScanner`` — per-item artifact verification (#129).

The candidate set is the open teatree ``Task`` rows (PENDING/CLAIMED) whose
ticket has an ``issue_url`` — never the harness TODO list. The sweep verifies
each task's artifact terminal state via the overlay's ``is_issue_done`` hook and
emits ``task.completion_detected`` only on durable proof, ``task.orphaned`` on
uncertainty (fail-OPEN, never auto-complete), and nothing for a genuinely-open
task. Idempotency rides an atomic ``last_sweep_check_ts`` conditional UPDATE.

Real Task/Ticket/Session rows against the test DB; only the code host
(``get_code_host_for_url``) is mocked — it is the unstoppable network external.
"""

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.task_sweep import TaskSweepScanner
from teatree.types import RawAPIDict

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


@dataclass
class _Host:
    issues_by_url: dict[str, RawAPIDict] = field(default_factory=dict)
    get_issue_calls: list[str] = field(default_factory=list)
    raise_on_fetch: bool = False

    def get_issue(self, issue_url: str) -> RawAPIDict:
        self.get_issue_calls.append(issue_url)
        if self.raise_on_fetch:
            msg = "network down"
            raise RuntimeError(msg)
        return self.issues_by_url.get(issue_url, {"error": "not found"})


class _Overlay(OverlayBase):
    """Issue is done when its ``state`` is ``closed``/``completed``."""

    def get_repos(self) -> list[str]:
        return ["acme-repo"]

    def get_provision_steps(self, worktree: Any) -> list:
        _ = worktree
        return []

    def is_issue_done(self, issue_data: dict[str, object]) -> bool:
        return issue_data.get("state") in {"closed", "completed", "merged"}


class _SweepHarness(TestCase):
    OVERLAY = "t3-acme"
    URL = "https://example.com/issues/100"

    def _scanner(self, *, recheck_interval_hours: int = 1) -> TaskSweepScanner:
        return TaskSweepScanner(
            overlay=_Overlay(),
            overlay_name=self.OVERLAY,
            recheck_interval_hours=recheck_interval_hours,
        )

    def _patch_host(self, host: _Host):
        return patch("teatree.loop.scanners.task_sweep.get_code_host_for_url", return_value=host)

    def _patch_no_host(self):
        return patch("teatree.loop.scanners.task_sweep.get_code_host_for_url", return_value=None)

    def _task(
        self,
        *,
        url: str | None = None,
        status: str = Task.Status.PENDING,
        overlay: str | None = None,
    ) -> Task:
        issue_url = self.URL if url is None else url
        ticket = Ticket.objects.create(overlay=overlay or self.OVERLAY, issue_url=issue_url)
        session = Session.objects.create(overlay=overlay or self.OVERLAY, ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        if status != Task.Status.PENDING:
            Task.objects.filter(pk=task.pk).update(status=status)
            task.refresh_from_db()
        return task


class CompletionDetectionTests(_SweepHarness):
    def test_terminal_issue_emits_completion(self) -> None:
        task = self._task()
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        with self._patch_host(host):
            signals = self._scanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "task.completion_detected"
        assert signals[0].payload["task_id"] == task.pk

    def test_open_issue_emits_nothing(self) -> None:
        self._task()
        host = _Host(issues_by_url={self.URL: {"state": "open"}})
        with self._patch_host(host):
            assert self._scanner().scan() == []

    def test_claimed_task_is_swept(self) -> None:
        self._task(status=Task.Status.CLAIMED)
        host = _Host(issues_by_url={self.URL: {"state": "merged"}})
        with self._patch_host(host):
            signals = self._scanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "task.completion_detected"

    def test_completed_task_is_not_swept(self) -> None:
        self._task(status=Task.Status.COMPLETED)
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        with self._patch_host(host):
            assert self._scanner().scan() == []
        assert host.get_issue_calls == []

    def test_failed_task_is_not_swept(self) -> None:
        self._task(status=Task.Status.FAILED)
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        with self._patch_host(host):
            assert self._scanner().scan() == []

    def test_task_without_issue_url_is_skipped(self) -> None:
        self._task(url="")
        host = _Host()
        with self._patch_host(host):
            assert self._scanner().scan() == []
        assert host.get_issue_calls == []

    def test_filters_by_overlay_name(self) -> None:
        self._task(overlay="other-overlay")
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        with self._patch_host(host):
            assert self._scanner().scan() == []

    def test_blank_overlay_name_sweeps_all_overlays(self) -> None:
        self._task(overlay="overlay-a", url=f"{self.URL}/a")
        self._task(overlay="overlay-b", url=f"{self.URL}/b")
        host = _Host(issues_by_url={f"{self.URL}/a": {"state": "closed"}, f"{self.URL}/b": {"state": "closed"}})
        scanner = TaskSweepScanner(overlay=_Overlay(), overlay_name="")
        with self._patch_host(host):
            signals = scanner.scan()
        assert len(signals) == 2

    def test_completion_payload_carries_task_and_ticket(self) -> None:
        task = self._task()
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        with self._patch_host(host):
            signals = self._scanner().scan()
        payload = signals[0].payload
        assert payload["task_id"] == task.pk
        assert payload["ticket_id"] == task.ticket.pk
        assert payload["issue_url"] == self.URL


class FailOpenTests(_SweepHarness):
    """Uncertainty never auto-completes — it emits ``task.orphaned``."""

    def test_network_error_emits_orphaned_not_completion(self) -> None:
        self._task()
        host = _Host(raise_on_fetch=True)
        with self._patch_host(host):
            signals = self._scanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "task.orphaned"

    def test_missing_code_host_emits_orphaned(self) -> None:
        self._task()
        with self._patch_no_host():
            signals = self._scanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "task.orphaned"

    def test_error_payload_emits_orphaned(self) -> None:
        self._task()
        host = _Host(issues_by_url={self.URL: {"error": "404 not found"}})
        with self._patch_host(host):
            signals = self._scanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "task.orphaned"

    def test_non_dict_issue_payload_emits_orphaned(self) -> None:
        self._task()
        host = _Host()
        host.issues_by_url = {}  # get_issue returns the {"error": ...} default
        with self._patch_host(host), patch.object(host, "get_issue", return_value=["not", "a", "dict"]):
            signals = self._scanner().scan()
        assert signals[0].kind == "task.orphaned"


class NeverBulkCompleteTests(_SweepHarness):
    """Each task is verified individually — no bulk completion path exists."""

    def test_three_tasks_each_verified_individually(self) -> None:
        urls = [f"{self.URL}/{i}" for i in range(3)]
        for url in urls:
            self._task(url=url)
        # Two terminal, one open — only the two terminal ones complete.
        host = _Host(
            issues_by_url={
                urls[0]: {"state": "closed"},
                urls[1]: {"state": "open"},
                urls[2]: {"state": "merged"},
            },
        )
        with self._patch_host(host):
            signals = self._scanner().scan()
        completed = [s for s in signals if s.kind == "task.completion_detected"]
        assert len(completed) == 2
        assert sorted(host.get_issue_calls) == sorted(urls), "every task fetched its own artifact"

    def test_scan_does_not_complete_tasks_itself(self) -> None:
        """The scanner only emits signals — it never mutates Task.status."""
        task = self._task()
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        with self._patch_host(host):
            self._scanner().scan()
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING, "the scanner must not complete the task; the handler does"


class IdempotencyTests(_SweepHarness):
    """``last_sweep_check_ts`` gates re-sweeps and serialises concurrent ticks."""

    def test_recently_swept_task_is_skipped(self) -> None:
        task = self._task()
        Task.objects.filter(pk=task.pk).update(last_sweep_check_ts=timezone.now() - _dt.timedelta(minutes=10))
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        with self._patch_host(host):
            assert self._scanner(recheck_interval_hours=1).scan() == []
        assert host.get_issue_calls == []

    def test_stale_swept_task_is_re_swept(self) -> None:
        task = self._task()
        Task.objects.filter(pk=task.pk).update(last_sweep_check_ts=timezone.now() - _dt.timedelta(hours=2))
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        with self._patch_host(host):
            signals = self._scanner(recheck_interval_hours=1).scan()
        assert len(signals) == 1

    def test_sweep_stamps_last_check_ts(self) -> None:
        task = self._task()
        host = _Host(issues_by_url={self.URL: {"state": "open"}})
        with self._patch_host(host):
            self._scanner().scan()
        task.refresh_from_db()
        assert task.last_sweep_check_ts is not None
        assert (timezone.now() - task.last_sweep_check_ts).total_seconds() < 60

    def test_concurrent_claim_lost_skips_verification(self) -> None:
        """If another tick stamps the row first, this tick does not verify it."""
        self._task()
        host = _Host(issues_by_url={self.URL: {"state": "closed"}})
        scanner = self._scanner()
        # Force the atomic claim to report 0 rows updated (a concurrent winner).
        with self._patch_host(host), patch.object(TaskSweepScanner, "_claim_for_sweep", return_value=False):
            assert scanner.scan() == []
        assert host.get_issue_calls == []


class PreMigrationResilienceTests(_SweepHarness):
    """A missing table/column tolerates the pre-migration window without crashing."""

    def test_candidate_query_operationalerror_returns_empty(self) -> None:
        from django.db import OperationalError  # noqa: PLC0415
        from django.db.models.query import QuerySet  # noqa: PLC0415

        self._task()
        host = _Host()
        scanner = self._scanner()
        with (
            self._patch_host(host),
            patch.object(QuerySet, "only", side_effect=OperationalError("no such column")),
        ):
            assert scanner.scan() == []

    def test_claim_operationalerror_is_lost_race(self) -> None:
        from django.db import OperationalError  # noqa: PLC0415
        from django.db.models.query import QuerySet  # noqa: PLC0415

        task = self._task()
        scanner = self._scanner()
        with patch.object(QuerySet, "update", side_effect=OperationalError("locked")):
            assert scanner._claim_for_sweep(task_id=task.pk, now=timezone.now()) is False


class ScannerNameTests(TestCase):
    def test_name_is_task_sweep(self) -> None:
        assert TaskSweepScanner(overlay=_Overlay()).name == "task_sweep"
