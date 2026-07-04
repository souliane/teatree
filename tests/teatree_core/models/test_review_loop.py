"""The bounded author<->reviewer ping-pong FSM (teatree#2298).

``ReviewLoop`` encodes the iterate-then-terminate review cycle as an
independent FSM (AUTHORING -> REVIEWING -> {PASSED, EXHAUSTED}), bounded by
``max_rounds``. The transition guards read the recorded verdict, never the
caller, so a HOLD verdict actually feeds the punch-list back to the author leg
(the pre-#2298 chokepoint left HOLD inert). The SELF variant records an
internal verdict on the row and posts nothing; the EXTERNAL variant binds a
real ``ReviewVerdict`` and reuses the proceed path on PASS.
"""

import pytest
from django.db.utils import IntegrityError
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.modelkit.phases import subagent_for_phase
from teatree.core.models import ReviewLoop, ReviewVerdict, Task, Ticket


def _complete(loop: ReviewLoop) -> None:
    """Drive the loop's current author leg to COMPLETED."""
    task = loop.current_task
    assert task is not None
    task.status = Task.Status.COMPLETED
    task.save(update_fields=["status"])


class TestSelfLoopStart(TestCase):
    def test_start_self_loop_schedules_first_author_leg(self) -> None:
        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_self_loop(ticket=ticket)

        assert loop.state == ReviewLoop.State.AUTHORING
        assert loop.variant == ReviewLoop.Variant.SELF
        assert loop.author_phase == "coding"
        assert loop.reviewer_phase == "reviewing"
        assert loop.current_task is not None
        assert loop.current_task.phase == "coding"

    def test_submit_for_review_dispatches_separate_reviewer(self) -> None:
        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_self_loop(ticket=ticket)
        author_task = loop.current_task
        _complete(loop)

        with self.captureOnCommitCallbacks(execute=True):
            loop.submit_for_review()
            loop.save()
        loop.refresh_from_db()

        assert loop.state == ReviewLoop.State.REVIEWING
        reviewer_task = ticket.tasks.get(phase="reviewing")
        assert reviewer_task.pk != author_task.pk
        author_agent = subagent_for_phase("author", loop.author_phase)
        reviewer_agent = subagent_for_phase("reviewer", loop.reviewer_phase)
        assert reviewer_agent
        assert reviewer_agent != author_agent


class TestVerdictDrivenTransitions(TestCase):
    def _reviewing_self_loop(self, *, max_rounds: int = 3) -> ReviewLoop:
        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_self_loop(ticket=ticket, max_rounds=max_rounds)
        _complete(loop)
        with self.captureOnCommitCallbacks(execute=True):
            loop.submit_for_review()
            loop.save()
        loop.refresh_from_db()
        return loop

    def test_pass_verdict_terminates_at_passed(self) -> None:
        loop = self._reviewing_self_loop()
        loop.latest_verdict_kind = ReviewLoop.VerdictKind.MERGE_SAFE
        loop.save(update_fields=["latest_verdict_kind"])

        before_round = loop.round
        with self.captureOnCommitCallbacks(execute=True):
            loop.pass_()
            loop.save()
        loop.refresh_from_db()

        assert loop.state == ReviewLoop.State.PASSED
        assert loop.round == before_round
        assert not loop.ticket.tasks.filter(phase="coding", status=Task.Status.PENDING).exists()

    def test_hold_iterates_then_pass_terminates(self) -> None:
        loop = self._reviewing_self_loop(max_rounds=3)
        first_author = loop.ticket.tasks.get(phase="coding")
        loop.latest_verdict_kind = ReviewLoop.VerdictKind.HOLD
        loop.latest_findings = [{"severity": "major", "summary": "missing edge-case test"}]
        loop.save(update_fields=["latest_verdict_kind", "latest_findings"])

        with self.captureOnCommitCallbacks(execute=True):
            loop.hold()
            loop.save()
        loop.refresh_from_db()

        assert loop.state == ReviewLoop.State.AUTHORING
        assert loop.round == 1
        second_author = loop.current_task
        assert second_author is not None
        assert second_author.pk != first_author.pk
        assert "missing edge-case test" in second_author.execution_reason

        _complete(loop)
        with self.captureOnCommitCallbacks(execute=True):
            loop.submit_for_review()
            loop.save()
        loop.refresh_from_db()
        loop.latest_verdict_kind = ReviewLoop.VerdictKind.MERGE_SAFE
        loop.save(update_fields=["latest_verdict_kind"])
        with self.captureOnCommitCallbacks(execute=True):
            loop.pass_()
            loop.save()
        loop.refresh_from_db()

        assert loop.state == ReviewLoop.State.PASSED

    def test_round_cap_exhausts_and_surfaces(self) -> None:
        loop = self._reviewing_self_loop(max_rounds=2)
        loop.latest_verdict_kind = ReviewLoop.VerdictKind.HOLD
        loop.save(update_fields=["latest_verdict_kind"])
        with self.captureOnCommitCallbacks(execute=True):
            loop.hold()
            loop.save()
        loop.refresh_from_db()
        assert loop.round == 1

        _complete(loop)
        with self.captureOnCommitCallbacks(execute=True):
            loop.submit_for_review()
            loop.save()
        loop.refresh_from_db()
        loop.latest_verdict_kind = ReviewLoop.VerdictKind.HOLD
        loop.save(update_fields=["latest_verdict_kind"])

        with self.captureOnCommitCallbacks(execute=True):
            loop.exhaust()
            loop.save()
        loop.refresh_from_db()

        assert loop.state == ReviewLoop.State.EXHAUSTED
        assert loop.needs_user_input is True
        assert loop.user_input_reason
        assert loop.ticket.tasks.filter(phase="coding").count() == 2

    def test_verdict_drives_transition_not_caller(self) -> None:
        loop = self._reviewing_self_loop()
        loop.latest_verdict_kind = ReviewLoop.VerdictKind.MERGE_SAFE
        loop.save(update_fields=["latest_verdict_kind"])
        with pytest.raises(TransitionNotAllowed):
            loop.hold()

        loop = self._reviewing_self_loop()
        loop.latest_verdict_kind = ReviewLoop.VerdictKind.HOLD
        loop.save(update_fields=["latest_verdict_kind"])
        with pytest.raises(TransitionNotAllowed):
            loop.pass_()


class TestIdempotentLegDispatch(TestCase):
    def test_idempotent_author_leg_dispatch(self) -> None:
        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_self_loop(ticket=ticket)
        loop.latest_findings = [{"severity": "minor", "summary": "rename symbol"}]

        with self.captureOnCommitCallbacks(execute=True):
            first = loop._schedule_author_leg(findings=loop.latest_findings)
            second = loop._schedule_author_leg(findings=loop.latest_findings)

        assert first.pk == second.pk
        assert loop.round_slots.filter(round=loop.round, leg=ReviewLoop.LEG_AUTHOR).count() == 1
        assert ticket.tasks.filter(phase="coding").count() == 1

    def test_round_leg_slot_unique_constraint(self) -> None:
        from teatree.core.models.review_loop import ReviewLoopRound  # noqa: PLC0415

        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_self_loop(ticket=ticket)
        with pytest.raises(IntegrityError):
            ReviewLoopRound.objects.create(review_loop=loop, round=loop.round, leg="author")


class TestSelfVariantNoEgress(TestCase):
    def test_self_variant_produces_no_external_verdict(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_self_loop(ticket=ticket, max_rounds=3)

        with patch("teatree.core.on_behalf_egress.OnBehalfSlackEgress.post") as post_spy:
            _complete(loop)
            with self.captureOnCommitCallbacks(execute=True):
                loop.submit_for_review()
                loop.save()
            loop.refresh_from_db()
            loop.latest_verdict_kind = ReviewLoop.VerdictKind.HOLD
            loop.latest_findings = [{"severity": "major", "summary": "fix it"}]
            loop.save(update_fields=["latest_verdict_kind", "latest_findings"])
            with self.captureOnCommitCallbacks(execute=True):
                loop.hold()
                loop.save()
            loop.refresh_from_db()
            _complete(loop)
            with self.captureOnCommitCallbacks(execute=True):
                loop.submit_for_review()
                loop.save()
            loop.refresh_from_db()
            loop.latest_verdict_kind = ReviewLoop.VerdictKind.MERGE_SAFE
            loop.save(update_fields=["latest_verdict_kind"])
            with self.captureOnCommitCallbacks(execute=True):
                loop.pass_()
                loop.save()
            loop.refresh_from_db()

        assert loop.state == ReviewLoop.State.PASSED
        assert ReviewVerdict.objects.count() == 0
        post_spy.assert_not_called()


class TestExternalLoopStart(TestCase):
    def test_start_external_loop_uses_e2e_phases(self) -> None:
        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_external_loop(ticket=ticket)

        assert loop.variant == ReviewLoop.Variant.EXTERNAL
        assert loop.author_phase == "e2e"
        assert loop.reviewer_phase == "e2e_reviewing"
        assert loop.current_task is not None
        assert loop.current_task.phase == "e2e"

    def test_external_reviewer_leg_returns_the_verdict_envelope(self) -> None:
        # corr-11: the external reviewer leg runs headless with no shell, so its
        # contract must instruct RETURNING the verdict, not running the CLI.
        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_external_loop(ticket=ticket)
        _complete(loop)
        with self.captureOnCommitCallbacks(execute=True):
            loop.submit_for_review()
            loop.save()

        reviewer_task = ticket.tasks.get(phase="e2e_reviewing")
        reason = reviewer_task.execution_reason
        assert "review_verdict" in reason
        assert "do NOT run `t3 review record`" in reason

    def test_external_pass_sets_proceed_marker(self) -> None:
        ticket = Ticket.objects.create()
        with self.captureOnCommitCallbacks(execute=True):
            loop = ReviewLoop.start_external_loop(ticket=ticket)
        _complete(loop)
        with self.captureOnCommitCallbacks(execute=True):
            loop.submit_for_review()
            loop.save()
        loop.refresh_from_db()

        verdict = ReviewVerdict.record(
            pr_id=7,
            slug="acme/repo",
            reviewed_sha="a" * 40,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
        )
        loop.latest_verdict = verdict
        loop.save(update_fields=["latest_verdict"])
        with self.captureOnCommitCallbacks(execute=True):
            loop.pass_()
            loop.save()
        loop.refresh_from_db()

        assert loop.state == ReviewLoop.State.PASSED
        assert loop.passed is True
