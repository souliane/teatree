"""Tick → DB persistence: kind=agent actions become Ticket + Task rows."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Task, Ticket
from teatree.loop.dispatch import DispatchAction
from teatree.loop.persistence import persist_agent_actions


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

    def test_short_verb_code_task_blocks_duplicate_coding_task(self) -> None:
        """#769 audit: _has_open_task must match any accepted phase spelling.

        A pre-existing short-verb ``code`` task (the unnormalized spelling
        ``tasks create <id> code`` stores) is an open coding task. Pre-fix,
        ``_has_open_task`` used a raw ``phase="coding"`` filter and missed
        it, so the orchestrator would create a *duplicate* coding task.
        """
        action = self._action()
        first = persist_agent_actions([action])
        assert len(first) == 1
        ticket = first[0].ticket
        # Re-stamp the existing task with the short-verb spelling and
        # reset the ticket so the auto-start path is re-evaluated.
        Task.objects.filter(ticket=ticket).update(phase="code", status=Task.Status.PENDING)
        ticket.state = Ticket.State.NOT_STARTED
        ticket.save()

        again = persist_agent_actions([action])

        assert again == [], (
            "a short-verb 'code' PENDING task did not block a duplicate coding task; _has_open_task compared raw phase"
        )
        assert Task.objects.filter(ticket=ticket).count() == 1


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
    """Completing the reviewing task on a reviewer ticket records the reviewed SHA + state on the ticket."""

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

        # The reviewer ticket's ``extra`` stamp doubles as the cache —
        # ReviewerPrsScanner reads it on the next tick to decide whether
        # to re-dispatch the reviewer agent.
        ticket = Ticket.objects.get(role=Ticket.Role.REVIEWER, issue_url="https://example.com/pr/7")
        assert ticket.extra["reviewed_sha"] == "zzz"
        assert ticket.extra["last_review_state"] == "approved"


class TestCrossOverlayLeak(TestCase):
    """Loop persistence attributes a ticket to its URL owner (#806, #743).

    The loop persistence path must attribute a ticket to the overlay that
    *owns* its URL, not the scanning overlay's tag — otherwise the ticket
    leaks into the wrong overlay's statusline zone and ``Ticket.save()``
    never corrects it (the explicit non-empty overlay disables
    ``_infer_overlay``). Incomplete-fix follow-up to #743.
    """

    _URL = "https://gitlab.example.com/team/widgets/-/merge_requests/7"

    def _reviewer_action(self, scan_tag: str) -> DispatchAction:
        return DispatchAction(
            kind="agent",
            zone="t3:reviewer",
            detail=f"Review needed: {self._URL}",
            payload={"url": self._URL, "head_sha": "deadbee", "previous_sha": "", "overlay": scan_tag},
        )

    def _orchestrator_action(self, scan_tag: str) -> DispatchAction:
        return DispatchAction(
            kind="agent",
            zone="t3:orchestrator",
            detail="Auto-start assigned issue",
            payload={"issue_url": self._URL, "auto_start": True, "overlay": scan_tag},
        )

    def test_reviewer_ticket_attributed_to_url_owner_not_scan_tag(self) -> None:
        # The scanning overlay is "gh"; the URL is owned by "gl".
        with patch(
            "teatree.core.overlay_loader.infer_overlay_for_url",
            return_value="gl",
        ):
            persist_agent_actions([self._reviewer_action(scan_tag="gh")])
        ticket = Ticket.objects.get(issue_url=self._URL)
        assert ticket.overlay == "gl", (
            f"reviewer ticket leaked into the scanning overlay's zone: "
            f"overlay={ticket.overlay!r}, expected 'gl' (the URL owner)"
        )

    def test_orchestrator_ticket_attributed_to_url_owner_not_scan_tag(self) -> None:
        with patch(
            "teatree.core.overlay_loader.infer_overlay_for_url",
            return_value="gl",
        ):
            persist_agent_actions([self._orchestrator_action(scan_tag="gh")])
        ticket = Ticket.objects.get(issue_url=self._URL)
        assert ticket.overlay == "gl"

    def test_falls_back_to_scan_tag_when_inference_inconclusive(self) -> None:
        # No registered overlay owns the URL → inference returns "" →
        # the scan tag is the only signal we have; keep it.
        with patch(
            "teatree.core.overlay_loader.infer_overlay_for_url",
            return_value="",
        ):
            persist_agent_actions([self._reviewer_action(scan_tag="gh")])
        ticket = Ticket.objects.get(issue_url=self._URL)
        assert ticket.overlay == "gh"

    def test_existing_misattributed_row_is_reconciled(self) -> None:
        # A pre-existing row persisted with the wrong (scanning) overlay.
        Ticket.objects.create(
            issue_url=self._URL,
            overlay="gh",
            role=Ticket.Role.REVIEWER,
        )
        with patch(
            "teatree.core.overlay_loader.infer_overlay_for_url",
            return_value="gl",
        ):
            persist_agent_actions([self._reviewer_action(scan_tag="gh")])
        ticket = Ticket.objects.get(issue_url=self._URL)
        assert ticket.overlay == "gl", (
            "an already-persisted cross-overlay-leaked row was not reconciled to its URL owner"
        )

    def test_inconclusive_inference_never_blanks_existing_attribution(self) -> None:
        # #743 invariant: an empty inference must not wipe a set overlay.
        Ticket.objects.create(
            issue_url=self._URL,
            overlay="gl",
            role=Ticket.Role.REVIEWER,
        )
        with patch(
            "teatree.core.overlay_loader.infer_overlay_for_url",
            return_value="",
        ):
            persist_agent_actions([self._reviewer_action(scan_tag="gh")])
        ticket = Ticket.objects.get(issue_url=self._URL)
        assert ticket.overlay == "gl"
