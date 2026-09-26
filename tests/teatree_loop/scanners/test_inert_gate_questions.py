"""Every undecided gate becomes ONE question, exactly once (16A).

The doctor has no hands: 51 files in ``cli/doctor/`` and not one of them creates a Ticket, a
Task or a Question, so twelve gates were printed at every session start for up to 85 days to a
human who had to notice and act. The alarm reached nothing.

The sink is ``DeferredQuestion``, not an issue: a decision is a question, and the owner has
capped factory-authored tickets. One question per GATE is the wrong grain — eleven of them sat
pending at once, which is the un-batched shape the owner objects to. The batch is keyed on the
SET of undecided gates, so an unchanged set asks once and a changed set is a new decision.
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.config.gate_evidence import ActivationIntent, GateEvidence, ObservableKind
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.inert_gate_questions import MARKER_PREFIX, InertGateQuestionScanner

_SHIPPED = dt.date(2020, 1, 1)
#: Real, shipped-OFF settings — ``enabled_anywhere`` reads the live field, so an invented key
#: would fail for the wrong reason and prove nothing about the finding split.
_UNDECIDED_SETTING = "require_merge_evidence"
_SECOND_UNDECIDED_SETTING = "require_executed_repro"
_STAGED_SETTING = "require_debt_delta"


def _entry(setting: str, intent: ActivationIntent) -> GateEvidence:
    return GateEvidence(
        setting=setting,
        off_value=False,
        kind=ObservableKind.NONE,
        target="",
        shipped=_SHIPPED,
        intent=intent,
        rationale="a refusal leaves no row behind",
        satisfier=f"t3 <overlay> config_setting set {setting} true",
    )


def _undecided(*settings: str) -> dict[str, GateEvidence]:
    return {setting: _entry(setting, ActivationIntent.UNDECIDED) for setting in settings}


@django.test.override_settings(USE_TZ=True)
class TestEveryUndecidedGateGoesIntoOneQuestion(django.test.TestCase):
    def setUp(self) -> None:
        DeferredQuestion.objects.all().delete()
        self.registry = _undecided(_UNDECIDED_SETTING, _SECOND_UNDECIDED_SETTING)

    def test_two_undecided_gates_file_a_single_question_naming_both(self) -> None:
        signals = InertGateQuestionScanner(registry=self.registry).scan()

        [question] = DeferredQuestion.objects.all()
        assert _UNDECIDED_SETTING in question.question
        assert _SECOND_UNDECIDED_SETTING in question.question
        assert question.dedupe_marker.startswith(MARKER_PREFIX)
        assert len(signals) == 1

    def test_the_question_carries_every_gate_satisfier(self) -> None:
        InertGateQuestionScanner(registry=self.registry).scan()

        [question] = DeferredQuestion.objects.all()
        assert f"config_setting set {_UNDECIDED_SETTING} true" in question.question
        assert f"config_setting set {_SECOND_UNDECIDED_SETTING} true" in question.question

    def test_the_signal_names_every_gate_it_batched(self) -> None:
        (signal,) = InertGateQuestionScanner(registry=self.registry).scan()

        assert signal.payload["settings"] == sorted(self.registry)

    def test_a_second_pass_raises_nothing(self) -> None:
        """The pin that prevents eleven of these — the escalate-once guard holds."""
        InertGateQuestionScanner(registry=self.registry).scan()
        InertGateQuestionScanner(registry=self.registry).scan()

        assert DeferredQuestion.objects.count() == 1

    def test_a_different_set_of_gates_is_a_different_decision(self) -> None:
        """Dedup on a fixed marker would silence the batch the day a new gate joined it."""
        InertGateQuestionScanner(registry=self.registry).scan()

        InertGateQuestionScanner(registry=_undecided(_UNDECIDED_SETTING)).scan()

        assert DeferredQuestion.objects.count() == 2

    def test_answering_it_lets_the_next_pass_ask_again(self) -> None:
        InertGateQuestionScanner(registry=self.registry).scan()
        DeferredQuestion.objects.update(answered_at=timezone.now(), answer_text="leave them off")

        InertGateQuestionScanner(registry=self.registry).scan()

        assert DeferredQuestion.objects.count() == 2


@django.test.override_settings(USE_TZ=True)
class TestADecisionAlreadyMadeRaisesNothing(django.test.TestCase):
    def setUp(self) -> None:
        DeferredQuestion.objects.all().delete()

    def test_a_staged_finding_files_nothing(self) -> None:
        registry = {_STAGED_SETTING: _entry(_STAGED_SETTING, ActivationIntent.STAGED)}

        signals = InertGateQuestionScanner(registry=registry).scan()

        assert DeferredQuestion.objects.count() == 0
        assert signals == []

    def test_a_staged_gate_beside_an_undecided_one_stays_out_of_the_question(self) -> None:
        registry = {
            _STAGED_SETTING: _entry(_STAGED_SETTING, ActivationIntent.STAGED),
            **_undecided(_UNDECIDED_SETTING),
        }

        (signal,) = InertGateQuestionScanner(registry=registry).scan()

        [question] = DeferredQuestion.objects.all()
        assert _STAGED_SETTING not in question.question
        assert signal.payload["settings"] == [_UNDECIDED_SETTING]
