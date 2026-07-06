"""Directive (north-star PR-6): the guarded intake FSM through ADMITTED.

The row IS the audit history: every state change is a guarded helper that raises on
an illegal transition. The load-bearing invariant — mirrored from
``OuterLoopExperiment.admit`` — is that ``admit`` is the ONLY writer of ``ADMITTED``
and RAISES without a consumed ratify question, so no code path can auto-admit a
directive (the structural human-in-the-loop of self-modification).
"""

import pytest
from django.test import TestCase

from teatree.core.models import DeferredQuestion, Directive, DirectiveError, FactoryScoreSnapshot, Ticket
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope


def _interpreted_directive() -> Directive:
    directive = Directive.objects.capture("max 1 MR per repo for overlay X", source=Directive.Source.CLI)
    directive.record_interpretation(sketch_from_envelope(valid_envelope()), constraint_statement="at most 1 open PR")
    return directive


def _snapshot() -> FactoryScoreSnapshot:
    return FactoryScoreSnapshot.objects.create(
        overlay="", window_days=7, recipe_sha="s", aggregate=0.7, verdict="ok", coverage=1.0, coverage_floor=0.6
    )


def _verifying_directive() -> "Directive":
    directive = _admitted_directive()
    directive.begin_implementation(
        Ticket.objects.create(issue_url="https://e.com/v", role=Ticket.Role.AUTHOR), baseline_snapshot=_snapshot()
    )
    directive.begin_configuring()
    directive.begin_verifying()
    return directive


class TestCapture(TestCase):
    def test_capture_records_a_captured_row_verbatim(self) -> None:
        directive = Directive.objects.capture("  always draft MRs for X  ", source=Directive.Source.CLI)
        assert directive.state == Directive.State.CAPTURED
        assert directive.raw_text == "always draft MRs for X"  # trimmed, otherwise verbatim
        assert directive.generation == 0

    def test_capture_refuses_blank_text(self) -> None:
        with pytest.raises(DirectiveError):
            Directive.objects.capture("   ", source=Directive.Source.CLI)


class TestInterpretationTransition(TestCase):
    def test_record_interpretation_moves_captured_to_interpreted_and_stores_the_sketch(self) -> None:
        directive = _interpreted_directive()
        assert directive.state == Directive.State.INTERPRETED
        assert directive.constraint_statement == "at most 1 open PR"
        assert directive.sketch is not None
        assert directive.sketch.setting_key == "max_open_prs_per_repo_per_ticket"

    def test_a_clarifying_directive_can_be_re_interpreted(self) -> None:
        directive = Directive.objects.capture("ambiguous directive", source=Directive.Source.CLI)
        directive.mark_clarifying()
        directive.bump_generation()
        directive.record_interpretation(sketch_from_envelope(valid_envelope()), constraint_statement="resolved")
        assert directive.state == Directive.State.INTERPRETED
        assert directive.generation == 1

    def test_mark_clarifying_is_idempotent_from_clarifying(self) -> None:
        directive = Directive.objects.capture("ambiguous", source=Directive.Source.CLI)
        directive.mark_clarifying()
        directive.mark_clarifying()  # a re-interpret returning MORE questions parks again, never raises
        assert directive.state == Directive.State.CLARIFYING


class TestAdmitIsHumanGated(TestCase):
    def test_admit_raises_without_a_consumed_ratify_question(self) -> None:
        # RED-before: there is NO self-admit path. A directive at RATIFY_PENDING whose
        # ratify question is unanswered cannot become ADMITTED.
        directive = _interpreted_directive()
        question = DeferredQuestion.record("Ratify?", options_hash="directive_ratify:test")
        directive.attach_ratification(question)
        assert directive.state == Directive.State.RATIFY_PENDING
        with pytest.raises(DirectiveError):
            directive.admit()
        directive.refresh_from_db()
        assert directive.state == Directive.State.RATIFY_PENDING

    def test_admit_succeeds_only_after_the_question_is_answered(self) -> None:
        directive = _interpreted_directive()
        question = DeferredQuestion.record("Ratify?", options_hash="directive_ratify:test2")
        directive.attach_ratification(question)
        DeferredQuestion.consume(question.pk, answer="approve")
        directive.refresh_from_db()
        directive.admit()
        assert directive.state == Directive.State.ADMITTED

    def test_attach_ratification_requires_the_interpreted_state(self) -> None:
        directive = Directive.objects.capture("not yet interpreted", source=Directive.Source.CLI)
        question = DeferredQuestion.record("Ratify?", options_hash="directive_ratify:test3")
        with pytest.raises(DirectiveError):
            directive.attach_ratification(question)


def _admitted_directive() -> Directive:
    directive = _interpreted_directive()
    question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
    directive.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="approve")
    directive.refresh_from_db()
    directive.admit()
    return directive


class TestPostAdmittedArc(TestCase):
    """The directive-loop arc past ADMITTED — guarded, mirroring OuterLoopExperiment."""

    def test_begin_implementation_binds_the_ticket_and_baseline(self) -> None:
        directive = _admitted_directive()
        ticket = Ticket.objects.create(issue_url="https://e.com/1", role=Ticket.Role.AUTHOR)
        baseline = _snapshot()
        directive.begin_implementation(ticket, baseline_snapshot=baseline)
        assert directive.state == Directive.State.IMPLEMENTING
        assert directive.ticket_id == ticket.pk
        assert directive.baseline_snapshot_id == baseline.pk

    def test_activation_only_skips_implementing_to_configuring(self) -> None:
        directive = _admitted_directive()
        directive.skip_to_configuring(baseline_snapshot=_snapshot())
        assert directive.state == Directive.State.CONFIGURING
        assert directive.ticket_id is None

    def test_begin_configuring_then_verifying_stamps_the_horizon_clock(self) -> None:
        directive = _admitted_directive()
        directive.begin_implementation(Ticket.objects.create(issue_url="https://e.com/2", role=Ticket.Role.AUTHOR))
        directive.begin_configuring()
        assert directive.state == Directive.State.CONFIGURING
        directive.begin_verifying()
        assert directive.state == Directive.State.VERIFYING
        assert directive.activation_applied_at is not None
        assert directive.verify_started_at is not None

    def test_record_fulfilled_from_verifying(self) -> None:
        directive = _verifying_directive()
        directive.record_fulfilled(reason="all five green")
        assert directive.state == Directive.State.FULFILLED
        assert directive.is_terminal

    def test_request_revert_from_verifying(self) -> None:
        directive = _verifying_directive()
        directive.request_revert(reason="collateral regression")
        assert directive.state == Directive.State.REVERT_PENDING
        assert not directive.is_terminal

    def test_request_revert_from_configuring_escalates_a_persistent_refusal(self) -> None:
        # A persistent configure refusal escalates to a human-asked revert from
        # CONFIGURING — never a soft-lock in perpetual waiting.
        directive = _admitted_directive()
        directive.begin_implementation(
            Ticket.objects.create(issue_url="https://e.com/cfg", role=Ticket.Role.AUTHOR),
            baseline_snapshot=_snapshot(),
        )
        directive.begin_configuring()
        directive.request_revert(reason="configure refused: read-back mismatch")
        assert directive.state == Directive.State.REVERT_PENDING

    def test_record_reverted_requires_a_consumed_revert_question(self) -> None:
        # RED-before: revert is human-ratified. A REVERT_PENDING directive whose
        # revert question is unanswered cannot reach REVERTED.
        directive = _verifying_directive()
        directive.request_revert(reason="regression")
        question = DeferredQuestion.record("Revert?", options_hash=f"directive_revert:{directive.pk}")
        directive.attach_revert_question(question)
        with pytest.raises(DirectiveError):
            directive.record_reverted()
        directive.refresh_from_db()
        assert directive.state == Directive.State.REVERT_PENDING
        DeferredQuestion.consume(question.pk, answer="reverted")
        directive.refresh_from_db()
        directive.record_reverted(revert_sha="deadbeef")
        assert directive.state == Directive.State.REVERTED
        assert directive.is_terminal
        assert directive.extra["revert_sha"] == "deadbeef"

    def test_begin_configuring_from_admitted_raises(self) -> None:
        directive = _admitted_directive()
        with pytest.raises(DirectiveError):
            directive.begin_configuring()

    def test_record_fulfilled_from_admitted_raises(self) -> None:
        directive = _admitted_directive()
        with pytest.raises(DirectiveError):
            directive.record_fulfilled(reason="premature")


class TestIllegalTransitionsRaise(TestCase):
    def test_admit_from_captured_raises(self) -> None:
        directive = Directive.objects.capture("x", source=Directive.Source.CLI)
        with pytest.raises(DirectiveError):
            directive.admit()

    def test_reject_terminalises_a_non_terminal_directive(self) -> None:
        directive = Directive.objects.capture("x", source=Directive.Source.CLI)
        directive.reject("uninterpretable")
        assert directive.state == Directive.State.REJECTED
        assert directive.is_terminal
        assert "uninterpretable" in directive.decision_reason

    def test_reject_refuses_a_terminal_directive(self) -> None:
        directive = Directive.objects.capture("x", source=Directive.Source.CLI)
        directive.reject("first")
        with pytest.raises(DirectiveError):
            directive.reject("again")

    def test_active_excludes_terminal_directives(self) -> None:
        live = Directive.objects.capture("live one", source=Directive.Source.CLI)
        dead = Directive.objects.capture("dead one", source=Directive.Source.CLI)
        dead.reject("done")
        active_pks = set(Directive.objects.active().values_list("pk", flat=True))
        assert live.pk in active_pks
        assert dead.pk not in active_pks
