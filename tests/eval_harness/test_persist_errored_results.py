"""An ungradeable trial is persisted as ``error``, never as a behavioral ``fail``.

``EvalScenarioResultQuerySet.graded()`` keys on the VERDICT, so a scenario whose
every trial died on an API 529 and landed as ``fail`` scores 0.0, stays in the
pass-rate math, and the next ``--gate-regressions`` run reports
``REGRESSED <scenario>: 1.00 -> 0.00`` for a network blip. The matrix path already
recorded ``error``; the pass@k path is the one the weekly CI lane drives, and the
single-trial path is the one every PR lane drives.
"""

from pathlib import Path

from django.test import TestCase

from teatree.core.models import EvalRunRecord
from teatree.eval.models import EvalRun, EvalSpec, Matcher, TokenUsage
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.persistence import persist_pass_at_k, persist_run
from teatree.eval.report import ScenarioResult

_MATCHER = Matcher(kind="positive", tool="Task", arg_path="prompt", operator="~", value="pytest")


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="text",
        agent_path="skills/rules/SKILL.md",
        prompt="do",
        matchers=(_MATCHER,),
        source_path=Path("/tmp/spec.yaml"),
    )


def _run(name: str, *, is_error: bool, terminal_reason: str = "success") -> EvalRun:
    return EvalRun(
        spec_name=name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.0,
        usage=TokenUsage(),
    )


def _trial(name: str, *, is_error: bool, terminal_reason: str = "success") -> ScenarioResult:
    return ScenarioResult(
        spec=_spec(name),
        run=_run(name, is_error=is_error, terminal_reason=terminal_reason),
        matcher_results=(),
        skipped=False,
    )


def _pass_at_k(name: str, trials: list[ScenarioResult], *, passes: int = 0) -> PassAtKResult:
    return PassAtKResult(
        spec_name=name,
        trials=len(trials),
        passes=passes,
        require="any",
        skipped=False,
        trial_results=tuple(trials),
    )


class TestPersistPassAtKErroredAggregate(TestCase):
    def test_every_trial_errored_persists_as_error_and_leaves_the_pass_rates(self) -> None:
        results = [
            _pass_at_k("alpha", [_trial("alpha", is_error=False)], passes=1),
            _pass_at_k("delegates_under_load", [_trial("delegates_under_load", is_error=True) for _ in range(3)]),
        ]

        record = persist_pass_at_k(results, model="m")

        persisted = {row.scenario_name: row for row in record.scenario_results.all()}
        assert persisted["delegates_under_load"].verdict == "error"
        assert persisted["delegates_under_load"].is_error is True
        assert persisted["alpha"].verdict == "pass"
        # The errored aggregate never reaches the pass-rate math, so the next
        # regression diff cannot report a 1.00 -> 0.00 drop for a transport blip.
        assert {rate.scenario_name for rate in record.pass_rates()} == {"alpha"}

    def test_one_clean_trial_keeps_the_aggregate_a_behavioral_fail(self) -> None:
        # The anti-vacuity control: a mixed aggregate carries real matcher evidence,
        # so it must stay a graded fail rather than hide behind the error verdict.
        trials = [_trial("mixed", is_error=True), _trial("mixed", is_error=False)]

        record = persist_pass_at_k([_pass_at_k("mixed", trials)], model="m")

        row = record.scenario_results.get()
        assert row.verdict == "fail"
        assert {rate.scenario_name for rate in record.pass_rates()} == {"mixed"}

    def test_the_aggregate_terminal_reason_is_recorded_for_triage(self) -> None:
        throttled = [_trial("beta", is_error=True, terminal_reason="throttled: 529 overloaded") for _ in range(2)]

        record = persist_pass_at_k([_pass_at_k("beta", throttled)], model="m")

        assert record.scenario_results.get().terminal_reason == "throttled: 529 overloaded"


class TestPersistSingleTrialErroredResult(TestCase):
    def test_an_errored_trial_persists_as_error_not_fail(self) -> None:
        results = [_trial("alpha", is_error=False), _trial("beta", is_error=True)]

        record = persist_run(results, model="m")

        persisted = {row.scenario_name: row.verdict for row in record.scenario_results.all()}
        assert persisted["beta"] == "error"
        assert {rate.scenario_name for rate in record.pass_rates()} == {"alpha"}


class TestErroredAggregateInTheRegressionDiff(TestCase):
    """The claim above is only true if the DIFF honours it — ``pass_rates()`` alone never did.

    ``regression_diff`` unions baseline and candidate scenario names, so an errored
    candidate scenario dropping out of ``pass_rates()`` is precisely what used to
    default it to ``0.0`` and print ``REGRESSED alpha: 1.00 -> 0.00``.
    """

    def test_an_all_errored_candidate_is_unmeasured_in_the_diff(self) -> None:
        baseline = persist_pass_at_k([_pass_at_k("alpha", [_trial("alpha", is_error=False)], passes=1)], model="m")
        candidate = persist_pass_at_k(
            [_pass_at_k("alpha", [_trial("alpha", is_error=True) for _ in range(3)])], model="m"
        )

        diff = {d.scenario_name: d for d in EvalRunRecord.regression_diff(baseline=baseline, candidate=candidate)}

        assert diff["alpha"].regressed is False
        assert diff["alpha"].unmeasured is True

    def test_a_graded_candidate_drop_is_still_a_regression(self) -> None:
        # Anti-vacuity: the diff must still catch a real behavioral drop.
        baseline = persist_pass_at_k([_pass_at_k("alpha", [_trial("alpha", is_error=False)], passes=1)], model="m")
        candidate = persist_pass_at_k([_pass_at_k("alpha", [_trial("alpha", is_error=False)], passes=0)], model="m")

        diff = {d.scenario_name: d for d in EvalRunRecord.regression_diff(baseline=baseline, candidate=candidate)}

        assert diff["alpha"].unmeasured is False
        assert diff["alpha"].regressed is True
