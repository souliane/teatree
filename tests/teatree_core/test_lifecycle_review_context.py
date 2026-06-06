"""Gate D: the `reviewing` phase needs recorded referenced-context retrieval.

Reviewing carries the same responsibility as implementing. The hole this
closes: ``lifecycle visit-phase <id> reviewing`` can be satisfied by a verdict
formed from the diff alone — no work item fetched, no links followed, no
referenced documents downloaded + analyzed. When a project opts in
(``require_review_context``), recording the ``reviewing`` attestation requires a
durable ``review_context`` artifact naming the fetched work item, listing a
downloaded reference, and recording its analysis. With the knob off the gate is
a NO-OP (opt-in default preserved).

The knob is pinned per test via ``review_context_required`` rather than the host
``~/.teatree.toml``, so the suite is deterministic regardless of the running
machine's config.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.config import UserSettings
from teatree.core.gates.review_context_gate import is_complete, recorded_review_context, review_context_required
from teatree.core.management.commands.lifecycle import ReviewContextError
from teatree.core.models import Session, Ticket
from teatree.core.models.task import Task

pytestmark = pytest.mark.django_db


@contextmanager
def _gate(*, required: bool) -> Iterator[None]:
    with patch("teatree.core.gates.review_context_gate.review_context_required", return_value=required):
        yield


class TestReviewingRequiresReviewContext(TestCase):
    def _ticket_ready_for_review(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        Session.objects.create(ticket=ticket, agent_id="maker:coding")
        return ticket

    def _visit_reviewing(self, ticket: Ticket) -> None:
        call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer")

    def _record_context(self, ticket: Ticket) -> None:
        ticket.record_review_context(
            work_item="https://gitlab.example.com/group/repo/-/issues/51",
            documents=["uploads/abc/Tilgungsplan.pdf"],
            analysis="amortization schedule matches the serializer rounding rules",
        )

    def test_refused_without_context_when_required(self) -> None:
        ticket = self._ticket_ready_for_review()
        with _gate(required=True), pytest.raises(ReviewContextError, match="referenced-context retrieval"):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        session.refresh_from_db()
        assert "reviewing" not in (session.visited_phases or [])

    def test_allowed_with_context_present(self) -> None:
        ticket = self._ticket_ready_for_review()
        self._record_context(ticket)
        with _gate(required=True):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases

    def test_noop_when_not_required(self) -> None:
        ticket = self._ticket_ready_for_review()
        with _gate(required=False):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases

    def test_partial_context_is_refused(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_context(work_item="https://x/issues/51", documents=[], analysis="looked at the diff")
        with _gate(required=True), pytest.raises(ReviewContextError, match="referenced-context retrieval"):
            self._visit_reviewing(ticket)


class TestReviewTransitionConditionIsMechanical(TestCase):
    """The constraint lives on the FSM transition, not only the CLI wrapper.

    ``review_context_satisfied`` is wired as a ``django_fsm`` condition on
    ``review()``, so the ``TESTED -> REVIEWED`` transition is mechanically
    refused (``TransitionNotAllowed``) when the gate is required and no context
    is recorded — regardless of the entry path. Exercised here at the
    predicate level (the CLI-path block is covered above): the condition is the
    single source the FSM consults.
    """

    def test_condition_false_without_context_when_required(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        with _gate(required=True):
            assert ticket.review_context_satisfied() is False

    def test_condition_true_with_context_when_required(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        ticket.record_review_context(work_item="https://x/51", documents=["s.pdf"], analysis="matches")
        with _gate(required=True):
            assert ticket.review_context_satisfied() is True

    def test_condition_true_when_not_required(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        with _gate(required=False):
            assert ticket.review_context_satisfied() is True

    def test_review_transition_consults_the_condition(self) -> None:
        meta = Ticket.review._django_fsm
        transition = meta.transitions[Ticket.State.TESTED]
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        with patch.object(ticket, "review_context_satisfied", return_value=False) as spy:
            results = [condition(ticket) for condition in transition.conditions]
        spy.assert_called_once_with()
        assert False in results


class TestNonFsmReviewPathsAreCovered(TestCase):
    """The constraint is path-independent: not only the lifecycle phase path.

    A review can be driven by a dynamic workflow (``Task.complete()`` ->
    ``mark_reviewed_externally`` / ``review``) or a direct ``t3:reviewer`` spawn
    that drives ``t3 ticket transition <id> review``. Both converge on the same
    ``review_context_satisfied`` FSM condition, so a verdict is mechanically
    refused on those paths too when the gate is required and no context exists.
    """

    def test_workflow_task_completion_review_is_refused_without_context(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="cold-reviewer")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="cold review",
        )
        with _gate(required=True), pytest.raises(TransitionNotAllowed):
            task.complete()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED

    def test_workflow_task_completion_review_allowed_with_context(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket, agent_id="cold-reviewer")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="cold review",
        )
        ticket.record_review_context(work_item="https://x/51", documents=["s.pdf"], analysis="matches")
        with _gate(required=True):
            task.complete()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_direct_cli_transition_review_returns_actionable_refusal(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        with _gate(required=True):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "transition", str(ticket.pk), "review"),
            )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        assert "record-review-context" in str(result["error"])


class TestRecordReviewContext(TestCase):
    def test_stores_work_item_documents_analysis_and_iso_timestamp(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        ticket.record_review_context(
            work_item="https://notion.so/work-item",
            documents=["spec.pdf", "design.md"],
            analysis="checked the business rules",
        )
        ticket.refresh_from_db()
        context = ticket.extra["review_context"]
        assert context["work_item"] == "https://notion.so/work-item"
        assert context["documents"] == ["spec.pdf", "design.md"]
        assert context["analysis"] == "checked the business rules"
        assert context["at"].endswith("+00:00") or context["at"].endswith("Z")

    def test_record_review_context_command_stamps_extra(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        call_command(
            "lifecycle",
            "record-review-context",
            str(ticket.pk),
            work_item="https://x/issues/51",
            documents="uploads/abc/schedule.pdf",
            analysis="schedule matches the diff",
        )
        ticket.refresh_from_db()
        assert ticket.extra["review_context"]["work_item"] == "https://x/issues/51"
        assert ticket.extra["review_context"]["documents"] == ["uploads/abc/schedule.pdf"]

    def test_record_review_context_command_refuses_partial_args(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        result = call_command(
            "lifecycle",
            "record-review-context",
            str(ticket.pk),
            work_item="https://x/issues/51",
            documents="",
            analysis="schedule matches the diff",
        )
        assert "refused" in result
        ticket.refresh_from_db()
        assert "review_context" not in (ticket.extra or {})


class TestReviewContextResolvers(TestCase):
    def test_review_context_required_reads_effective_settings(self) -> None:
        with patch(
            "teatree.core.gates.review_context_gate.get_effective_settings",
            return_value=UserSettings(require_review_context=True),
        ):
            assert review_context_required() is True

    def test_recorded_review_context_empty_without_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        assert recorded_review_context(ticket) == {}

    def test_is_complete_requires_all_three_parts(self) -> None:
        assert is_complete({"work_item": "x", "documents": ["d"], "analysis": "a"}) is True
        assert is_complete({"work_item": "", "documents": ["d"], "analysis": "a"}) is False
        assert is_complete({"work_item": "x", "documents": [], "analysis": "a"}) is False
        assert is_complete({"work_item": "x", "documents": ["d"], "analysis": ""}) is False
        assert is_complete({"work_item": "x", "documents": ["  "], "analysis": "a"}) is False
