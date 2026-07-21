"""Escalation-ladder baseline generation — opus runs ONLY on sonnet's failures.

Pure-core tests over :func:`teatree.eval.ladder.run_escalation_ladder`: a synthetic
``run_trial`` records every ``(scenario, model)`` dispatch, so the ladder's
"never touch a costlier tier once a cheaper one passed" contract is asserted on
the dispatch record itself — no metered run, no API.
"""

from collections import Counter
from pathlib import Path

import pytest

from teatree.agents.model_tiering import TIER_MODELS
from teatree.eval.ladder import LadderPolicy, laddered_tier_models, resolve_ladder_tiers, run_escalation_ladder
from teatree.eval.models import EvalRun, EvalSpec, Matcher
from teatree.eval.report import MatcherResult, ScenarioResult

_HAIKU = TIER_MODELS["cheap"]
_SONNET = TIER_MODELS["balanced"]
_OPUS = TIER_MODELS["frontier"]
_LADDER = [_HAIKU, _SONNET, _OPUS]


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name, scenario="sc", agent_path="skills/code/SKILL.md", prompt="p", matchers=(), source_path=Path("x.yaml")
    )


def _run(*, skipped: bool = False) -> EvalRun:
    return EvalRun(
        spec_name="x",
        tool_calls=(),
        text_blocks=(),
        terminal_reason="skipped: test" if skipped else "end_turn",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def _result(spec: EvalSpec, *, passed: bool, skipped: bool = False) -> ScenarioResult:
    if skipped:
        return ScenarioResult(spec=spec, run=_run(skipped=True), matcher_results=(), skipped=True)
    matcher = Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x")
    return ScenarioResult(
        spec=spec,
        run=_run(),
        matcher_results=(MatcherResult(matcher=matcher, passed=passed, message="" if passed else "no match"),),
        skipped=False,
    )


class _RecordingTrial:
    """A ``run_trial`` whose verdict is a canned per-``(scenario, model, trial)`` script.

    ``verdicts[(name, model)]`` is the ordered list of per-trial ``bool``/``"skip"``
    outcomes; a scenario/model absent from the map defaults to a single failing
    trial. Every call is appended to :attr:`dispatched`, so a test asserts on the
    exact set of tiers the ladder dispatched.
    """

    def __init__(self, verdicts: dict[tuple[str, str], list[object]]) -> None:
        self._verdicts = verdicts
        self._cursor: Counter[tuple[str, str]] = Counter()
        self.dispatched: list[tuple[str, str]] = []

    def __call__(self, spec: EvalSpec) -> ScenarioResult:
        key = (spec.name, spec.model)
        self.dispatched.append(key)
        script = self._verdicts.get(key, [False])
        index = min(self._cursor[key], len(script) - 1)
        self._cursor[key] += 1
        outcome = script[index]
        if outcome == "skip":
            return _result(spec, passed=False, skipped=True)
        return _result(spec, passed=bool(outcome))

    def models_for(self, name: str) -> list[str]:
        return [model for (n, model) in self.dispatched if n == name]


class TestLadderedTierModels:
    def test_orders_the_three_tier_models_cheapest_first(self) -> None:
        assert laddered_tier_models() == [_HAIKU, _SONNET, _OPUS]


class TestNoDispatchAboveTheFirstPass:
    def test_haiku_pass_dispatches_neither_sonnet_nor_opus(self) -> None:
        trial = _RecordingTrial({("alpha", _HAIKU): [True]})
        rows = run_escalation_ladder([_spec("alpha")], _LADDER, run_trial=trial)
        assert trial.models_for("alpha") == [_HAIKU]
        assert _SONNET not in trial.models_for("alpha")
        assert _OPUS not in trial.models_for("alpha")
        assert [(r.model, r.passed) for r in rows] == [(_HAIKU, True)]

    def test_sonnet_pass_never_dispatches_opus(self) -> None:
        trial = _RecordingTrial({("alpha", _HAIKU): [False], ("alpha", _SONNET): [True]})
        rows = run_escalation_ladder([_spec("alpha")], _LADDER, run_trial=trial)
        assert trial.models_for("alpha") == [_HAIKU, _SONNET]
        assert _OPUS not in trial.models_for("alpha")
        assert [(r.model, r.passed) for r in rows] == [(_HAIKU, False), (_SONNET, True)]

    def test_a_skip_stops_escalation_and_is_not_a_capability_failure(self) -> None:
        trial = _RecordingTrial({("alpha", _HAIKU): ["skip"]})
        rows = run_escalation_ladder([_spec("alpha")], _LADDER, run_trial=trial)
        assert trial.models_for("alpha") == [_HAIKU]
        assert rows[0].skipped is True
        assert rows[0].passed is False


class TestAllFailIsSurfacedNotTieredToFrontier:
    def test_failing_every_tier_records_three_failed_rows_none_passed(self) -> None:
        trial = _RecordingTrial({("alpha", _HAIKU): [False], ("alpha", _SONNET): [False], ("alpha", _OPUS): [False]})
        rows = run_escalation_ladder([_spec("alpha")], _LADDER, run_trial=trial)
        assert trial.models_for("alpha") == [_HAIKU, _SONNET, _OPUS]
        assert all(not r.passed for r in rows)
        # Not silently tiered to frontier: no row is recorded as a PASS at opus.
        assert not any(r.passed for r in rows if r.model == _OPUS)


class TestTrialsGating:
    def test_all_n_pass_is_required_before_a_tier_counts_as_passed(self) -> None:
        # haiku passes trial 1 but fails trial 2 → under require=all the tier FAILS,
        # so the ladder escalates to sonnet (which passes both).
        trial = _RecordingTrial({("alpha", _HAIKU): [True, False], ("alpha", _SONNET): [True, True]})
        rows = run_escalation_ladder(
            [_spec("alpha")], _LADDER, run_trial=trial, policy=LadderPolicy(trials=2, require="all")
        )
        assert trial.models_for("alpha") == [_HAIKU, _HAIKU, _SONNET, _SONNET]
        assert [(r.model, r.passed) for r in rows] == [(_HAIKU, False), (_SONNET, True)]

    def test_all_n_pass_at_the_cheapest_tier_stops_immediately(self) -> None:
        trial = _RecordingTrial({("alpha", _HAIKU): [True, True]})
        rows = run_escalation_ladder(
            [_spec("alpha")], _LADDER, run_trial=trial, policy=LadderPolicy(trials=2, require="all")
        )
        assert trial.models_for("alpha") == [_HAIKU, _HAIKU]
        assert _SONNET not in trial.models_for("alpha")
        assert rows[0].passed is True
        assert rows[0].trials == 2


class TestMultipleScenariosEscalateIndependently:
    def test_each_scenario_walks_its_own_ladder(self) -> None:
        trial = _RecordingTrial(
            {
                ("cheap_ok", _HAIKU): [True],
                ("needs_sonnet", _HAIKU): [False],
                ("needs_sonnet", _SONNET): [True],
                ("needs_opus", _HAIKU): [False],
                ("needs_opus", _SONNET): [False],
                ("needs_opus", _OPUS): [True],
            }
        )
        specs = [_spec("cheap_ok"), _spec("needs_sonnet"), _spec("needs_opus")]
        run_escalation_ladder(specs, _LADDER, run_trial=trial)
        assert trial.models_for("cheap_ok") == [_HAIKU]
        assert trial.models_for("needs_sonnet") == [_HAIKU, _SONNET]
        assert trial.models_for("needs_opus") == [_HAIKU, _SONNET, _OPUS]


class TestResolveLadderTiers:
    def test_maps_each_scenario_to_its_cheapest_passing_model(self) -> None:
        trial = _RecordingTrial(
            {
                ("cheap_ok", _HAIKU): [True],
                ("needs_sonnet", _HAIKU): [False],
                ("needs_sonnet", _SONNET): [True],
                ("needs_opus", _HAIKU): [False],
                ("needs_opus", _SONNET): [False],
                ("needs_opus", _OPUS): [True],
                ("all_fail", _HAIKU): [False],
                ("all_fail", _SONNET): [False],
                ("all_fail", _OPUS): [False],
            }
        )
        specs = [_spec("cheap_ok"), _spec("needs_sonnet"), _spec("needs_opus"), _spec("all_fail")]
        rows = run_escalation_ladder(specs, _LADDER, run_trial=trial)
        assert resolve_ladder_tiers(rows) == {
            "cheap_ok": _HAIKU,
            "needs_sonnet": _SONNET,
            "needs_opus": _OPUS,
            "all_fail": None,
        }

    def test_a_skipped_scenario_has_no_tier(self) -> None:
        trial = _RecordingTrial({("alpha", _HAIKU): ["skip"]})
        rows = run_escalation_ladder([_spec("alpha")], _LADDER, run_trial=trial)
        assert resolve_ladder_tiers(rows) == {"alpha": None}


class TestRejectsBadPolicy:
    def test_trials_below_one_is_rejected(self) -> None:
        trial = _RecordingTrial({("alpha", _HAIKU): [True]})
        with pytest.raises(ValueError, match="k must be"):
            run_escalation_ladder([_spec("alpha")], _LADDER, run_trial=trial, policy=LadderPolicy(trials=0))
