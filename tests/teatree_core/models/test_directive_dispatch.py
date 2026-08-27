"""DirectiveDispatch (north-star PR-6): the idempotent enqueue of the headless interpreter.

Mirrors ``CriticDispatch``: one row per ``(directive, purpose, generation)`` linking a
claimable headless ``Task(phase="directive_interpreting")``. A re-fire at the same
generation returns ``None`` (no second interpreter); a bumped generation arms a fresh
one. The interpret task needs a ``Ticket``, so the dispatch anchors a synthetic one.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.core.models import Directive, DirectiveDispatch, Task
from teatree.core.models.directive_dispatch import INTERPRET_PHASE, MAX_INTERPRET_ATTEMPTS
from teatree.core.models.task_handoff import schedule_resume


def _directive() -> Directive:
    return Directive.objects.capture("max 1 MR per repo for overlay X", source=Directive.Source.CLI)


class TestDirectiveDispatchEnqueue(TestCase):
    def test_enqueue_creates_a_headless_interpret_task(self) -> None:
        row = DirectiveDispatch.enqueue(directive=_directive(), contract="interpret this directive")
        assert row is not None
        assert row.task is not None
        # Its OWN phase so the result is measured against the interpret evidence
        # contract; the execution lane is the runtime's routing decision (Task.save).
        assert row.task.phase == "directive_interpreting"
        assert "interpret this directive" in row.task.execution_reason

    def test_enqueue_anchors_a_synthetic_ticket_for_the_interpret_task(self) -> None:
        directive = _directive()
        row = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert row is not None
        assert row.task is not None
        assert row.task.ticket is not None
        assert f"directive={directive.pk}" in row.task.ticket.issue_url

    def test_enqueue_is_idempotent_per_generation(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        second = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert second is None  # the same generation arms no second interpreter
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 1

    def test_a_bumped_generation_waits_on_a_live_interpreter_then_arms_a_fresh_one(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        directive.bump_generation()
        # Liveness spans generations: the generation-0 interpreter is still claimable.
        assert DirectiveDispatch.enqueue(directive=directive, contract="c") is None

        first.task.complete()
        second = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert second is not None
        assert second.generation == 1
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 2

    def test_the_recorder_reaches_the_directive_from_the_task(self) -> None:
        # The reverse link the server-side recorder walks: task -> dispatch -> directive.
        directive = _directive()
        row = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert row is not None
        assert row.task is not None
        assert row.task.directive_dispatches.first().directive_id == directive.pk


class TestDirectiveDispatchReArm(TestCase):
    """A prior interpreter that recorded no interpretation must not strand the directive.

    The dedup that keeps a re-tick from spawning a second interpreter must hold ONLY
    while an interpreter is in flight. Once the prior interpret task reaches a terminal
    status without an interpretation being recorded — the governor refused it and the
    artifact sweep completed the PENDING task, or the run itself failed the evidence
    gate — the directive is still awaiting interpretation, so a re-tick RE-ARMS a fresh
    interpreter rather than dedup-returning ``None`` forever.
    """

    def test_a_live_pending_interpreter_is_not_rearmed(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        assert first.task.status == Task.Status.PENDING  # in flight
        assert DirectiveDispatch.enqueue(directive=directive, contract="c") is None  # dedup holds

    def test_a_completed_interpreter_that_recorded_nothing_is_rearmed(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        old_task = first.task
        old_task.complete()  # the observed defect: swept-complete with no envelope
        assert directive.state == Directive.State.CAPTURED  # never advanced past intake

        rearmed = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert rearmed is not None  # NOT dedup-suppressed
        assert rearmed.task is not None
        assert rearmed.task.pk != old_task.pk  # a fresh interpreter task
        assert rearmed.task.status == Task.Status.PENDING
        # Same generation → one dispatch row, its task re-pointed (no unique-constraint churn).
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 1
        first.refresh_from_db()
        assert first.task_id == rearmed.task.pk

    def test_a_failed_interpreter_that_recorded_nothing_is_rearmed(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        first.task.fail(reason="missing required evidence: bad envelope")  # run-then-fail
        rearmed = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert rearmed is not None
        assert rearmed.task is not None
        assert rearmed.task.status == Task.Status.PENDING

    def test_rearm_leaves_a_bumped_generation_as_its_own_fresh_row(self) -> None:
        # A clarification bump is orthogonal to re-arm: it still arms its own row.
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        first.task.complete()
        directive.bump_generation()
        second = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert second is not None
        assert second.generation == 1
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 2


def _live_interpret_tasks(ticket_id: int) -> list[Task]:
    return list(Task.objects.filter(ticket_id=ticket_id, phase=INTERPRET_PHASE, status__in=Task.Status.active()))


class TestDirectiveDispatchSecondProducer(TestCase):
    """A resume task is a second producer of interpreters that owns no dispatch row.

    Answering a parked ``needs_user_input`` question fires ``schedule_resume``
    on the parked interpret task, which creates a fresh ``directive_interpreting`` task
    with no dispatch row — invisible to a dedup that inspects only its own row's task.
    The measured result was two live claimable interpreters on one synthetic ticket.
    """

    def test_a_resume_task_on_the_same_ticket_blocks_a_fresh_dispatch(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        resume = schedule_resume(first.task, answer="the clarification answer")
        first.task.complete()  # the parked interpreter is terminal; only the resume is live

        assert DirectiveDispatch.enqueue(directive=directive, contract="c") is None
        assert _live_interpret_tasks(resume.ticket_id) == [resume]

    def test_a_resume_task_blocks_a_bumped_generation_dispatch(self) -> None:
        # The measured pair: a resume at generation 0 and a `reinterpret_after_clarification`
        # dispatch at generation 1, both claimable on the same synthetic ticket.
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        resume = schedule_resume(first.task, answer="the clarification answer")
        first.task.complete()
        directive.bump_generation()

        assert DirectiveDispatch.enqueue(directive=directive, contract="c") is None
        assert _live_interpret_tasks(resume.ticket_id) == [resume]

    def test_a_live_interpreter_on_another_ticket_does_not_block(self) -> None:
        # The negative control: liveness is scoped to THIS directive's synthetic ticket.
        other = DirectiveDispatch.enqueue(directive=_directive(), contract="c")
        assert other is not None
        row = DirectiveDispatch.enqueue(directive=_directive(), contract="c")
        assert row is not None
        assert row.task is not None
        assert other.task is not None
        assert row.task.ticket_id != other.task.ticket_id


class TestDirectiveDispatchAttemptBudget(TestCase):
    """The re-arm is rationed: a directive no interpreter can read is parked, not retried forever."""

    def test_the_rearm_stops_at_the_attempt_budget(self) -> None:
        directive = _directive()
        for _ in range(MAX_INTERPRET_ATTEMPTS):
            row = DirectiveDispatch.enqueue(directive=directive, contract="c")
            assert row is not None
            assert row.task is not None
            row.task.fail(reason="missing required evidence: bad envelope")

        assert DirectiveDispatch.enqueue(directive=directive, contract="c") is None
        directive.refresh_from_db()
        assert directive.state == Directive.State.REJECTED
        assert str(MAX_INTERPRET_ATTEMPTS) in directive.decision_reason

    def test_a_human_resume_does_not_spend_the_dispatch_budget(self) -> None:
        # A resume is the human's answer landing, not the loop re-arming — it must not
        # count against the budget that bounds the loop's own retries.
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        for _ in range(MAX_INTERPRET_ATTEMPTS + 2):
            resume = schedule_resume(first.task, answer="a")
            resume.complete()
        first.task.fail(reason="no envelope")

        assert DirectiveDispatch.enqueue(directive=directive, contract="c") is not None


def _first_select(statements: list[str], needle: str) -> int | None:
    return next((i for i, sql in enumerate(statements) if sql.startswith("SELECT") and needle in sql), None)


class TestDirectiveDispatchLockOrder(TestCase):
    """The dedup decision is TAKEN under the lock, not merely followed by one.

    Deciding liveness before the lock is a check two concurrent ticks both pass: T1
    takes the lock, arms an interpreter and commits, then T2 takes it and re-points the
    row at a second claimable one. Two live interpreters on one synthetic ticket — the
    measured defect the dedup exists to prevent — with the row tracking only the second.
    """

    def test_the_directive_row_is_locked_before_the_liveness_probe_runs(self) -> None:
        directive = _directive()
        with CaptureQueriesContext(connection) as captured:
            assert DirectiveDispatch.enqueue(directive=directive, contract="c") is not None

        statements = [entry["sql"] for entry in captured.captured_queries]
        lock_at = _first_select(statements, '"teatree_directive"')
        probe_at = _first_select(statements, INTERPRET_PHASE)
        assert lock_at is not None, "the directive row is never read under lock"
        assert probe_at is not None
        assert lock_at < probe_at

    def test_the_budget_probe_also_runs_under_the_lock(self) -> None:
        directive = _directive()
        with CaptureQueriesContext(connection) as captured:
            assert DirectiveDispatch.enqueue(directive=directive, contract="c") is not None

        statements = [entry["sql"] for entry in captured.captured_queries]
        lock_at = _first_select(statements, '"teatree_directive"')
        count_at = _first_select(statements, "COUNT")
        assert lock_at is not None, "the directive row is never read under lock"
        assert count_at is not None
        assert lock_at < count_at
