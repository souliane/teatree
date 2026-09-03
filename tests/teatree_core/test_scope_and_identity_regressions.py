"""Row-scoped reads must key on the ROW, and identity fields must mean one thing.

Four small defects of the same family:

*   ``overlay_scope_q`` admitted a task as a pre-multi-overlay legacy row whenever
    EITHER relation was blank, so an ``acme`` ticket's task — whose session is
    never stamped — was visible and claimable by every other overlay;
*   ``reap_pre_gate`` read the ACTIVE overlay's ownership settings while judging
    another overlay's worktree, so an all-overlay sweep could delete a
    colleague-owned worktree the row's own overlay protects;
*   ``waiting`` published a question's pk in ``entry_id``, the field the CLI
    ``resolve`` command acts on — so ``waiting resolve 1`` closed an unrelated
    manual item;
*   the lifecycle plan scored a reviewer terminal state (``REVIEW_POSTED``) at
    the same order as a ticket that does not exist, reporting intake as its
    current step.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.cleanup import reap_pre_gates
from teatree.core.lifecycle_pipeline import DriveSeams, TicketSnapshot, drive
from teatree.core.models import DeferredQuestion, Session, Task, Ticket, Worktree
from teatree.core.waiting import WaitingKind, gather_waiting

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestTaskOverlayScopeAdmitsOnlyTrueLegacyRows(TestCase):
    def _task(self, *, ticket_overlay: str, session_overlay: str) -> Task:
        ticket = Ticket.objects.create(
            overlay=ticket_overlay,
            issue_url=f"https://example.com/issues/{ticket_overlay or 'legacy'}",
        )
        session = Session.objects.create(ticket=ticket, overlay=session_overlay, agent_id="loop")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.PENDING,
        )

    def test_a_foreign_overlays_task_is_not_visible_through_a_blank_session(self) -> None:
        task = self._task(ticket_overlay="acme", session_overlay="")
        assert list(Task.objects.for_overlay("t3-teatree")) == []
        assert list(Task.objects.for_overlay("acme")) == [task]

    def test_a_genuinely_legacy_row_is_still_visible_everywhere(self) -> None:
        task = self._task(ticket_overlay="", session_overlay="")
        assert list(Task.objects.for_overlay("t3-teatree")) == [task]
        assert list(Task.objects.for_overlay("acme")) == [task]


class TestSessionInheritsItsTicketsOverlay(TestCase):
    def test_a_session_created_without_an_overlay_is_stamped_from_its_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/9")
        assert Session.objects.create(ticket=ticket, agent_id="loop").overlay == "acme"

    def test_an_explicit_overlay_is_never_overwritten(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/10")
        assert Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="loop").overlay == "t3-teatree"


class TestReapGateReadsTheRowsOwnOverlay(TestCase):
    def test_ownership_settings_are_resolved_for_the_worktree_row(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/11")
        worktree = Worktree.objects.create(
            overlay="acme", ticket=ticket, repo_path="product", branch="feat-x", extra={}
        )
        seen: list[str | None] = []

        def _record(overlay: str | None = None) -> object:
            seen.append(overlay)
            return get_effective_settings(None)

        with (
            patch.object(reap_pre_gates, "get_effective_settings", side_effect=_record),
            patch.object(reap_pre_gates, "is_clean_ignored", return_value=False),
            patch.object(reap_pre_gates, "resolve_clone_path", return_value=Path("/tmp/clone")),
        ):
            reap_pre_gates.reap_pre_gate(worktree, workspace=Path("/tmp/ws"))

        assert seen == ["acme"]


class TestQuestionsAreNotManuallyResolvable(TestCase):
    def test_a_pending_question_publishes_no_resolvable_entry_id(self) -> None:
        DeferredQuestion.objects.create(question="which branch?")
        entries = [e for e in gather_waiting("t3-teatree") if e.kind == WaitingKind.QUESTION]
        assert entries
        assert all(entry.entry_id is None for entry in entries)


class TestLifecyclePlanTreatsAnUnrankedStateAsOffPath:
    def _drive(self, state: str) -> object:
        snapshot = TicketSnapshot(exists=True, state=state, provisioned=True, ignored=False)
        seams = DriveSeams(
            snapshot_provider=lambda: snapshot,
            ticket_id_provider=lambda: 1,
            chokepoint_runner=lambda _step: "",
        )
        return drive("1", seams, plan_only=True)

    def test_a_reviewer_terminal_state_is_off_path_not_absent(self) -> None:
        report = self._drive(Ticket.State.REVIEW_POSTED)
        assert report.stopped_reason == "off_path"
        assert report.stopped_at is None

    def test_a_golden_path_state_still_plans_normally(self) -> None:
        assert self._drive(Ticket.State.STARTED).stopped_reason == "pending"
