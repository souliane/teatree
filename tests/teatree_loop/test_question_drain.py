"""Every pending ``DeferredQuestion`` is reachable by some automated resolver (#4178).

Before this, exactly one auto-drain existed and its ``dedupe_marker__startswith="repair-"``
filter reached 6 of 70 pending rows: 52 carried no marker at all, so nothing could key a
subject off them and they waited on a human, one at a time. These tests pin the three
things that closes:

* a subject derived from ``parked_task`` / a numeric ``session_id``, not just the marker;
* a registry where every pending row gets a decision from some resolver;
* an age backstop that records a state transition rather than letting a row sit silently.

The over-resolve guard is pinned throughout: a live or undeterminable subject is KEPT.
"""

from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.loop.question_drain import DrainReport, Verdict, drain_pending_questions, question_reachability
from teatree.loop.tick_recovery import _reap_stale_task_claims


def _ticket(state: str = Ticket.State.STARTED) -> Ticket:
    return Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)


def _parked_question(*, ticket_state: str) -> DeferredQuestion:
    """A marker-less row correlated to its subject only by ``parked_task``."""
    ticket = _ticket(ticket_state)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    task = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.FAILED)
    return DeferredQuestion.record("How should this park proceed?", parked_task=task)


def _session_keyed_question(*, ticket_state: str) -> DeferredQuestion:
    """A marker-less row whose ``session_id`` is a stringified ``Session`` pk."""
    ticket = _ticket(ticket_state)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    return DeferredQuestion.record("How should this session proceed?", session_id=str(session.pk))


def _age(question: DeferredQuestion, *, days: int) -> None:
    DeferredQuestion.objects.filter(pk=question.pk).update(created_at=timezone.now() - timedelta(days=days))


class TestSubjectDerivedDrain(TestCase):
    def test_markerless_parked_question_drains_on_terminal_subject(self) -> None:
        # The #4178 headline: no dedupe_marker at all, so the repair-only filter never
        # selected it — the row waited on a human even though its ticket had merged.
        question = _parked_question(ticket_state=Ticket.State.MERGED)

        report = drain_pending_questions()

        assert report.drained == 1
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED
        assert question.resolved_via == DeferredQuestion.ResolvedVia.STALE

    def test_markerless_parked_question_kept_on_live_subject(self) -> None:
        question = _parked_question(ticket_state=Ticket.State.STARTED)

        report = drain_pending_questions()

        assert report.drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_session_keyed_question_drains_on_terminal_subject(self) -> None:
        question = _session_keyed_question(ticket_state=Ticket.State.DELIVERED)

        assert drain_pending_questions().drained == 1
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED

    def test_session_keyed_question_kept_on_live_subject(self) -> None:
        question = _session_keyed_question(ticket_state=Ticket.State.CODED)

        assert drain_pending_questions().drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_harness_uuid_session_is_not_a_subject(self) -> None:
        # A harness session id is not a Session pk — deriving a subject from it would
        # drain a live owner question on a coincidence. Underivable ⇒ kept.
        question = DeferredQuestion.record("A real owner decision", session_id="a0e4ab27-26ec-41bc-bb72-2a140141762f")

        assert drain_pending_questions().drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_session_id_naming_no_session_row_is_kept(self) -> None:
        question = DeferredQuestion.record("A real owner decision", session_id="999999")

        assert drain_pending_questions().drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_subjectless_question_is_never_drained(self) -> None:
        question = DeferredQuestion.record("Which colour should the button be?")

        assert drain_pending_questions().drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_drain_is_idempotent(self) -> None:
        _parked_question(ticket_state=Ticket.State.MERGED)

        assert drain_pending_questions().drained == 1
        assert drain_pending_questions().drained == 0

    def test_an_empty_backlog_is_a_no_op(self) -> None:
        assert drain_pending_questions() == DrainReport(drained=0, escalated=0)

    def test_escalating_a_row_a_concurrent_answer_resolved_is_not_counted(self) -> None:
        question = DeferredQuestion.record("A real owner decision")
        DeferredQuestion.consume(question.pk, answer="answered first")

        assert question.mark_escalated("too late") is False


class TestReachability(TestCase):
    def test_an_empty_backlog_reports_nothing(self) -> None:
        assert question_reachability() == []

    def test_a_derivable_subject_is_reachable_by_a_subject_resolver(self) -> None:
        # On main a marker-less row was reachable by NO resolver; the reachability
        # report is what makes that measurable instead of anecdotal.
        parked = _parked_question(ticket_state=Ticket.State.STARTED)
        session_keyed = _session_keyed_question(ticket_state=Ticket.State.STARTED)

        by_id = {reach.question_id: reach for reach in question_reachability()}

        assert by_id[parked.pk].has_subject
        assert by_id[parked.pk].decisions["subject_terminal"] == Verdict.KEEP
        assert by_id[session_keyed.pk].has_subject
        assert by_id[session_keyed.pk].decisions["subject_terminal"] == Verdict.KEEP

    def test_a_subjectless_row_reports_no_subject_but_still_reaches_the_backstop(self) -> None:
        question = DeferredQuestion.record("Which colour should the button be?")
        _age(question, days=9)

        reach = next(r for r in question_reachability() if r.question_id == question.pk)

        assert not reach.has_subject
        assert "subject_terminal" not in reach.decisions
        assert reach.decisions["age_ceiling"] == Verdict.ESCALATE

    def test_every_pending_row_is_reachable_by_some_resolver(self) -> None:
        _parked_question(ticket_state=Ticket.State.MERGED)
        _session_keyed_question(ticket_state=Ticket.State.STARTED)
        _age(DeferredQuestion.record("A stale owner decision"), days=30)

        unreachable = [reach.question_id for reach in question_reachability() if not reach.decisions]

        assert unreachable == []


class TestAgeBackstop(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("deferred_question_age_ceiling_days", 3)

    def test_a_row_past_the_ceiling_is_escalated(self) -> None:
        question = DeferredQuestion.record("A real owner decision")
        _age(question, days=5)

        report = drain_pending_questions()

        assert report.escalated == 1
        question.refresh_from_db()
        assert question.escalated_at is not None
        assert question.escalation_count == 1
        assert question.status == DeferredQuestion.STATUS_PENDING
        assert DeferredQuestionAudit.objects.filter(question=question, action="escalated").count() == 1

    def test_a_young_row_is_not_escalated(self) -> None:
        question = DeferredQuestion.record("A real owner decision")
        _age(question, days=1)

        assert drain_pending_questions().escalated == 0
        question.refresh_from_db()
        assert question.escalated_at is None
        assert question.escalation_count == 0

    def test_escalation_is_rate_limited_to_one_per_window(self) -> None:
        question = DeferredQuestion.record("A real owner decision")
        _age(question, days=5)

        assert drain_pending_questions().escalated == 1
        assert drain_pending_questions().escalated == 0
        question.refresh_from_db()
        assert question.escalation_count == 1

    def test_escalation_repeats_once_the_window_elapses(self) -> None:
        question = DeferredQuestion.record("A real owner decision")
        _age(question, days=5)
        assert drain_pending_questions().escalated == 1
        DeferredQuestion.objects.filter(pk=question.pk).update(escalated_at=timezone.now() - timedelta(days=4))

        assert drain_pending_questions().escalated == 1
        question.refresh_from_db()
        assert question.escalation_count == 2

    def test_escalation_never_resolves_a_row(self) -> None:
        # Directive #45: an unresolved request is never silently dropped. The backstop
        # records a transition; it must never dismiss an owner question on age alone.
        question = DeferredQuestion.record("A real owner decision")
        _age(question, days=90)

        report = drain_pending_questions()

        assert report.drained == 0
        question.refresh_from_db()
        assert question.is_pending

    def test_a_drained_row_is_not_also_escalated(self) -> None:
        question = _parked_question(ticket_state=Ticket.State.MERGED)
        _age(question, days=30)

        report = drain_pending_questions()

        assert (report.drained, report.escalated) == (1, 0)

    def test_a_live_subject_past_the_ceiling_is_still_escalated(self) -> None:
        # KEEP is not an excuse to sit forever: the backstop runs on every row the
        # subject stage did not drain, including one it explicitly kept.
        question = _parked_question(ticket_state=Ticket.State.STARTED)
        _age(question, days=5)

        report = drain_pending_questions()

        assert (report.drained, report.escalated) == (0, 1)


class TestAgeBackstopDisabled(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("deferred_question_age_ceiling_days", 0)

    def test_a_zero_ceiling_disables_escalation(self) -> None:
        question = DeferredQuestion.record("A real owner decision")
        _age(question, days=90)

        assert drain_pending_questions().escalated == 0
        question.refresh_from_db()
        assert question.escalated_at is None


class TestSweepCost(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("deferred_question_age_ceiling_days", 3)

    def _sweep_queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            drain_pending_questions()
        return len(captured.captured_queries)

    def test_the_sweep_cost_does_not_grow_with_the_backlog(self) -> None:
        # Everything one sweep needs — the subject index, the clock, the ceiling — is
        # resolved ONCE. Resolving the effective settings per row made a 40-deep backlog
        # cost 82 queries; it also re-read the clock per row, so the cutoff drifted
        # WITHIN a single sweep and two equally-old rows could decide differently.
        DeferredQuestion.record("Question 0")
        one_row = self._sweep_queries()

        for i in range(1, 20):
            DeferredQuestion.record(f"Question {i}")

        assert self._sweep_queries() == one_row


class TestTickWiring(TestCase):
    def test_the_tick_recovery_sweep_drives_the_drain(self) -> None:
        question = _parked_question(ticket_state=Ticket.State.MERGED)

        _reap_stale_task_claims()

        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED
