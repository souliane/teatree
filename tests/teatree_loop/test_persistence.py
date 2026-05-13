"""Tick → DB persistence: kind=agent actions become Ticket + Task rows."""

import json
import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.models import Task, Ticket
from teatree.loop.dispatch import DispatchAction
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.scanners import reviewer_prs


class TestPersistReviewer(TestCase):
    def _action(
        self,
        *,
        url: str = "https://example.com/owner/repo/pull/42",
        head_sha: str = "abc123",
        overlay: str = "acme",
    ) -> DispatchAction:
        return DispatchAction(
            kind="agent",
            zone="t3:reviewer",
            detail=f"Review needed: {url}",
            payload={"url": url, "head_sha": head_sha, "previous_sha": "", "overlay": overlay},
        )

    def test_creates_reviewer_ticket_and_reviewing_task(self) -> None:
        created = persist_agent_actions([self._action()])

        assert len(created) == 1
        task = created[0]
        assert task.phase == "reviewing"
        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        ticket = task.ticket
        assert ticket.role == Ticket.Role.REVIEWER
        assert ticket.issue_url == "https://example.com/owner/repo/pull/42"
        assert ticket.overlay == "acme"
        assert ticket.extra == {"reviewed_sha": "abc123"}

    def test_is_idempotent_within_one_call(self) -> None:
        action = self._action()
        created = persist_agent_actions([action, action])
        # Both actions point to the same URL+SHA → one Ticket, one Task.
        assert len(created) == 1
        assert Ticket.objects.filter(issue_url=action.payload["url"]).count() == 1
        assert Task.objects.filter(ticket__issue_url=action.payload["url"]).count() == 1

    def test_is_idempotent_across_calls(self) -> None:
        action = self._action()
        persist_agent_actions([action])
        second = persist_agent_actions([action])
        # Open reviewing task already exists → no new Task created.
        assert second == []
        assert Task.objects.filter(ticket__issue_url=action.payload["url"]).count() == 1

    def test_updates_reviewed_sha_when_author_pushes(self) -> None:
        first = self._action(head_sha="abc123")
        persist_agent_actions([first])
        # Author pushed new commits; complete the prior task so a new one can be scheduled.
        Task.objects.filter(ticket__issue_url=first.payload["url"]).update(status="completed")
        second = self._action(head_sha="def456")
        created = persist_agent_actions([second])

        assert len(created) == 1
        ticket = Ticket.objects.get(issue_url=first.payload["url"])
        assert ticket.extra["reviewed_sha"] == "def456"

    def test_skips_action_without_url(self) -> None:
        action = DispatchAction(kind="agent", zone="t3:reviewer", detail="no url", payload={})
        assert persist_agent_actions([action]) == []
        assert Ticket.objects.count() == 0

    def test_does_not_promote_author_ticket_to_reviewer(self) -> None:
        url = "https://example.com/owner/repo/pull/42"
        Ticket.objects.create(issue_url=url, overlay="acme", role=Ticket.Role.AUTHOR)
        result = persist_agent_actions([self._action(url=url)])

        assert result == []  # Existing author ticket is not converted.
        assert Ticket.objects.get(issue_url=url).role == Ticket.Role.AUTHOR


class TestPersistOrchestrator(TestCase):
    def _action(
        self,
        *,
        issue_url: str = "https://example.com/owner/repo/issues/99",
        overlay: str = "acme",
        auto_start: bool = True,
    ) -> DispatchAction:
        return DispatchAction(
            kind="agent",
            zone="t3:orchestrator",
            detail="Auto-start assigned issue",
            payload={"issue_url": issue_url, "auto_start": auto_start, "overlay": overlay},
        )

    def test_creates_author_ticket_and_coding_task(self) -> None:
        created = persist_agent_actions([self._action()])

        assert len(created) == 1
        task = created[0]
        assert task.phase == "coding"
        ticket = task.ticket
        assert ticket.role == Ticket.Role.AUTHOR
        assert ticket.issue_url == "https://example.com/owner/repo/issues/99"

    def test_skips_when_auto_start_is_false(self) -> None:
        result = persist_agent_actions([self._action(auto_start=False)])
        assert result == []
        assert Ticket.objects.count() == 0

    def test_skips_pending_task_signal_without_issue_url(self) -> None:
        # pending_task signals also dispatch to t3:orchestrator but the Task already exists.
        action = DispatchAction(
            kind="agent",
            zone="t3:orchestrator",
            detail="pending task",
            payload={"task_id": 42},  # no issue_url, no auto_start
        )
        assert persist_agent_actions([action]) == []


class TestPersistIgnoredKinds(TestCase):
    def test_ignores_non_agent_actions(self) -> None:
        action = DispatchAction(
            kind="statusline",
            zone="in_flight",
            detail="PR open",
            payload={"url": "https://example.com/pr/1"},
        )
        assert persist_agent_actions([action]) == []
        assert Ticket.objects.count() == 0

    def test_ignores_unknown_agent_zone(self) -> None:
        action = DispatchAction(
            kind="agent",
            zone="t3:unknown",
            detail="?",
            payload={"url": "https://example.com/x"},
        )
        assert persist_agent_actions([action]) == []
        assert Ticket.objects.count() == 0


class TestReviewerCacheUpdate(TestCase):
    """Completing the reviewing task on a reviewer ticket updates the scanner cache."""

    def setUp(self) -> None:
        super().setUp()
        self._cache_tmp = tempfile.TemporaryDirectory()
        self._cache_path = Path(self._cache_tmp.name) / "reviewer_prs.json"
        self._original_default_path = reviewer_prs._default_cache_path
        reviewer_prs._default_cache_path = lambda: self._cache_path  # ty: ignore[invalid-assignment]

    def tearDown(self) -> None:
        reviewer_prs._default_cache_path = self._original_default_path
        self._cache_tmp.cleanup()
        super().tearDown()

    def test_mark_reviewed_externally_writes_scanner_cache(self) -> None:
        action = DispatchAction(
            kind="agent",
            zone="t3:reviewer",
            detail="Review",
            payload={"url": "https://example.com/pr/7", "head_sha": "zzz", "overlay": "acme"},
        )
        created = persist_agent_actions([action])
        assert len(created) == 1
        task = created[0]
        task.complete()

        assert self._cache_path.is_file()
        data = json.loads(self._cache_path.read_text())
        # New schema records both the head sha and the reviewer's state
        # so the next scan can detect approval dismissals on force-push.
        assert data == {"https://example.com/pr/7": {"sha": "zzz", "state": "approved"}}
