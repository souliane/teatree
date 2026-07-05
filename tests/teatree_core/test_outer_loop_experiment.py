"""The T4 autoresearch experiment ledger + its guarded state machine (T4-PR-3).

Integration-first against the real DB: every state helper is a guarded transition
that raises on an illegal source state, and :meth:`OuterLoopExperiment.admit` /
:meth:`record_reverted` additionally RAISE unless a consumed (answered)
DeferredQuestion FK exists — the structural human-in-the-loop the outer loop
relies on (there is no auto-admit / auto-revert writer to assert against).
"""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import (
    DeferredQuestion,
    FactoryScoreSnapshot,
    OuterLoopExperiment,
    OuterLoopExperimentError,
    ProposalSpec,
    Ticket,
)


def _snapshot(*, aggregate: float | None = 0.7, overlay: str = "") -> FactoryScoreSnapshot:
    return FactoryScoreSnapshot.objects.create(
        overlay=overlay,
        window_days=7,
        recipe_sha="abc",
        aggregate=aggregate,
        verdict="ok",
        coverage=1.0,
        coverage_floor=0.6,
    )


def _answered_question(answer: str = "approve") -> DeferredQuestion:
    question = DeferredQuestion.record("Ratify experiment?", options_hash="h")
    DeferredQuestion.consume(question.pk, answer=answer)
    return DeferredQuestion.objects.get(pk=question.pk)


class TestPropose(TestCase):
    def test_propose_creates_a_proposed_experiment(self) -> None:
        exp = _make_experiment(
            hypothesis="Raise review_catch by tightening the review gate.",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.SIGNAL_REGRESSION,
            regress_band=0.05,
        )
        assert exp.state == OuterLoopExperiment.State.PROPOSED
        assert exp.target_provider_id == "review_catch"
        assert not exp.is_terminal

    def test_blank_hypothesis_is_refused(self) -> None:
        with pytest.raises(OuterLoopExperimentError):
            _make_experiment(
                hypothesis="   ",
                target_provider_id="review_catch",
                source=OuterLoopExperiment.Source.OPERATOR,
            )

    def test_round_trips_all_fields(self) -> None:
        baseline = _snapshot()
        exp = _make_experiment(
            hypothesis="H",
            target_provider_id="merge_latency",
            source=OuterLoopExperiment.Source.CORE_GAP,
            overlay="acme",
            regress_band=0.1,
            baseline_snapshot=baseline,
        )
        reloaded = OuterLoopExperiment.objects.get(pk=exp.pk)
        assert reloaded.overlay == "acme"
        assert reloaded.baseline_snapshot_id == baseline.pk
        assert reloaded.regress_band == pytest.approx(0.1)
        assert str(reloaded).startswith("outer-loop-experiment<")


class TestRatifyGate(TestCase):
    def _proposed(self) -> OuterLoopExperiment:
        return _make_experiment(
            hypothesis="H",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.SIGNAL_REGRESSION,
        )

    def test_admit_requires_a_consumed_ratify_question(self) -> None:
        # The core structural invariant: an experiment cannot reach ADMITTED
        # without a consumed (answered) DeferredQuestion — there is no auto-admit
        # writer, so admit() itself raises when the gate is unmet.
        exp = self._proposed()
        pending = DeferredQuestion.record("Ratify?", options_hash="h")
        exp.attach_ratification(pending)
        assert exp.state == OuterLoopExperiment.State.RATIFY_PENDING
        with pytest.raises(OuterLoopExperimentError):
            exp.admit()
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.RATIFY_PENDING

    def test_admit_succeeds_after_the_question_is_answered(self) -> None:
        exp = self._proposed()
        answered = _answered_question()
        exp.attach_ratification(answered)
        exp.admit()
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.ADMITTED

    def test_ratify_denial_rejects(self) -> None:
        exp = self._proposed()
        exp.attach_ratification(DeferredQuestion.record("Ratify?"))
        exp.reject("operator declined")
        reloaded = OuterLoopExperiment.objects.get(pk=exp.pk)
        assert reloaded.state == OuterLoopExperiment.State.REJECTED
        assert reloaded.decision == OuterLoopExperiment.Decision.REJECT
        assert reloaded.is_terminal


class TestFullPipeline(TestCase):
    def _admitted(self) -> OuterLoopExperiment:
        exp = _make_experiment(
            hypothesis="H",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.SIGNAL_REGRESSION,
            baseline_snapshot=_snapshot(),
        )
        exp.attach_ratification(_answered_question())
        exp.admit()
        return exp

    def test_kept_path_binds_the_merged_sha(self) -> None:
        exp = self._admitted()
        exp.begin_implementation(_ticket())
        exp.arm_measure()
        assert exp.measure_started_at is not None
        exp.record_kept(post_snapshot=_snapshot(aggregate=0.85), merged_sha="deadbeef", reason="improved")
        reloaded = OuterLoopExperiment.objects.get(pk=exp.pk)
        assert reloaded.state == OuterLoopExperiment.State.KEPT
        assert reloaded.merged_sha == "deadbeef"
        assert reloaded.decision == OuterLoopExperiment.Decision.KEEP

    def test_revert_path_is_gated_on_a_consumed_revert_question(self) -> None:
        exp = self._admitted()
        exp.begin_implementation(_ticket())
        exp.arm_measure()
        exp.request_revert(post_snapshot=_snapshot(aggregate=0.6), reason="no improvement")
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.REVERT_PENDING
        exp.attach_revert_question(DeferredQuestion.record("Revert?"))
        with pytest.raises(OuterLoopExperimentError):
            exp.record_reverted(revert_sha="cafe")
        # Once the revert question is answered, the revert lands.
        answered = _answered_question()
        exp.revert_question = answered
        exp.save(update_fields=["revert_question"])
        exp.record_reverted(revert_sha="cafe")
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.REVERTED


class TestIllegalTransitions(TestCase):
    def _proposed(self) -> OuterLoopExperiment:
        return _make_experiment(
            hypothesis="H",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.OPERATOR,
        )

    def test_each_helper_raises_from_a_wrong_state(self) -> None:
        exp = self._proposed()  # PROPOSED
        with pytest.raises(OuterLoopExperimentError):
            exp.admit()
        with pytest.raises(OuterLoopExperimentError):
            exp.begin_implementation(_ticket())
        with pytest.raises(OuterLoopExperimentError):
            exp.arm_measure()
        with pytest.raises(OuterLoopExperimentError):
            exp.record_kept(post_snapshot=_snapshot(), merged_sha="x", reason="r")
        with pytest.raises(OuterLoopExperimentError):
            exp.request_revert(post_snapshot=_snapshot(), reason="r")
        with pytest.raises(OuterLoopExperimentError):
            exp.attach_revert_question(DeferredQuestion.record("Q"))
        with pytest.raises(OuterLoopExperimentError):
            exp.record_reverted(revert_sha="x")

    def test_reject_refuses_a_terminal_experiment(self) -> None:
        exp = self._proposed()
        exp.reject("done")
        with pytest.raises(OuterLoopExperimentError):
            exp.reject("again")

    def test_attach_ratification_only_from_proposed(self) -> None:
        exp = self._proposed()
        exp.attach_ratification(_answered_question())
        with pytest.raises(OuterLoopExperimentError):
            exp.attach_ratification(DeferredQuestion.record("Q"))


class TestManagerQueries(TestCase):
    def test_active_and_weekly_counts(self) -> None:
        _make_experiment(hypothesis="H1", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR)
        old = _make_experiment(
            hypothesis="H2", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR
        )
        OuterLoopExperiment.objects.filter(pk=old.pk).update(created_at=timezone.now() - dt.timedelta(days=30))
        assert OuterLoopExperiment.objects.active_count() == 2
        assert OuterLoopExperiment.objects.weekly_count() == 1

    def test_consecutive_non_kept_stops_at_first_kept(self) -> None:
        def _terminal(state: str) -> None:
            exp = _make_experiment(
                hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR
            )
            OuterLoopExperiment.objects.filter(pk=exp.pk).update(state=state)

        _terminal(OuterLoopExperiment.State.KEPT)
        _terminal(OuterLoopExperiment.State.REJECTED)
        _terminal(OuterLoopExperiment.State.REVERTED)
        # Trailing two are non-KEPT; the KEPT before them stops the count.
        assert OuterLoopExperiment.objects.consecutive_non_kept() == 2


def _ticket() -> Ticket:
    return Ticket.objects.create(
        issue_url=f"https://example.com/issue/{timezone.now().timestamp()}",
        role=Ticket.Role.AUTHOR,
    )


def _make_experiment(
    *,
    overlay: str = "",
    baseline_snapshot: FactoryScoreSnapshot | None = None,
    **spec_kw: object,
) -> OuterLoopExperiment:
    """Build an experiment via the ProposalSpec factory (test convenience)."""
    return OuterLoopExperiment.objects.propose(
        ProposalSpec(**spec_kw), overlay=overlay, baseline_snapshot=baseline_snapshot
    )
