"""pass@k aggregates multiple trials of a scenario."""

from pathlib import Path

import pytest

from teatree.eval.models import EvalRun, EvalSpec, Matcher
from teatree.eval.pass_at_k import run_pass_at_k
from teatree.eval.report import ScenarioResult


def _spec() -> EvalSpec:
    return EvalSpec(
        name="flaky",
        scenario="text",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=Path("/tmp/spec.yaml"),
    )


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _result(  # noqa: PLR0913 — test-data builder: each kwarg maps 1:1 to an EvalRun field a case varies.
    spec: EvalSpec,
    *,
    passed: bool,
    skipped: bool = False,
    cost_usd: float = 0.0,
    terminal_reason: str | None = None,
    fell_back: bool | None = None,
    main_cost_usd: float = 0.0,
    aux_cost_usd: float = 0.0,
) -> ScenarioResult:
    reason = terminal_reason if terminal_reason is not None else ("skipped: x" if skipped else "success")
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason=reason,
        is_error=not passed and not skipped,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost_usd,
        fell_back=fell_back,
        main_cost_usd=main_cost_usd,
        aux_cost_usd=aux_cost_usd,
    )
    return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=skipped)


def _sequence_runner(spec: EvalSpec, verdicts: list[bool]):
    it = iter(verdicts)

    def _run(_spec: EvalSpec) -> ScenarioResult:
        return _result(spec, passed=next(it))

    return _run


class TestRunPassAtK:
    def test_any_passes_when_one_trial_passes(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [False, True, False]), k=3, require="any")
        assert result.ok
        assert result.passes == 1
        assert result.trials == 3

    def test_all_fails_when_one_trial_fails(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [True, False, True]), k=3, require="all")
        assert not result.ok
        assert result.passes == 2

    def test_all_passes_when_every_trial_passes(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [True, True]), k=2, require="all")
        assert result.ok
        assert result.passes == result.trials

    def test_any_fails_when_no_trial_passes(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [False, False]), k=2, require="any")
        assert not result.ok
        assert result.passes == 0

    def test_all_trials_skipped_marks_skipped_and_ok(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, lambda s: _result(s, passed=False, skipped=True), k=3)
        assert result.skipped
        assert result.ok

    def test_rejects_bad_k(self) -> None:
        spec = _spec()
        with pytest.raises(ValueError, match="k must be"):
            run_pass_at_k(spec, lambda s: _result(s, passed=True), k=0)

    def test_rejects_bad_require(self) -> None:
        spec = _spec()
        with pytest.raises(ValueError, match="require must be"):
            run_pass_at_k(spec, lambda s: _result(s, passed=True), k=1, require="most")

    def test_sums_cost_across_every_trial(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, lambda s: _result(s, passed=True, cost_usd=0.25), k=3)
        assert result.cost_usd == pytest.approx(0.75)

    def test_clean_when_every_trial_completed_cleanly(self) -> None:
        # cost_usd/usage are SUMMED across trials, so the aggregated cell is
        # "clean" (its billed identity holds) only when EVERY trial finished
        # cleanly — terminal_reason then stays empty (not a cap reason).
        spec = _spec()
        result = run_pass_at_k(spec, lambda s: _result(s, passed=True, terminal_reason="success"), k=3)
        assert result.terminal_reason == ""

    def test_capped_trial_marks_the_aggregated_cell_capped(self) -> None:
        # If ANY trial hit a cap reason, the summed cost mixes a partial/aborted
        # trial in — the aggregated cell must carry a cap terminal_reason so the
        # benchmark's `_clean_cost_cells` excludes it from the rate fit.
        spec = _spec()
        it = iter(["success", "budget_exceeded", "success"])
        result = run_pass_at_k(spec, lambda s: _result(s, passed=True, terminal_reason=next(it)), k=3)
        assert result.terminal_reason == "budget_exceeded"

    def test_sums_main_and_aux_cost_across_trials(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, lambda s: _result(s, passed=True, main_cost_usd=0.5, aux_cost_usd=0.02), k=3)
        assert result.main_cost_usd == pytest.approx(1.5)
        assert result.aux_cost_usd == pytest.approx(0.06)

    def test_any_fallback_trial_makes_the_aggregated_cell_fell_back(self) -> None:
        spec = _spec()
        it = iter([False, True, False])
        result = run_pass_at_k(spec, lambda s: _result(s, passed=True, fell_back=next(it)), k=3)
        assert result.fell_back is True

    def test_all_clean_trials_make_the_aggregated_cell_not_fell_back(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, lambda s: _result(s, passed=True, fell_back=False), k=3)
        assert result.fell_back is False

    def test_unobservable_trials_leave_fell_back_none(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, lambda s: _result(s, passed=True, fell_back=None), k=3)
        assert result.fell_back is None

    def test_cap_truncated_trial_does_not_count_as_a_pass(self) -> None:
        # A trial whose partial trajectory satisfied its matchers but that hit a
        # cap (max_turns/budget) must NOT increment the pass count in the GATING
        # lane — raising caps (#19) would otherwise mask a real failure as green.
        spec = _spec()
        it = iter(["success", "max_turns", "success"])

        def _run(_spec: EvalSpec) -> ScenarioResult:
            return _result(spec, passed=True, terminal_reason=next(it))

        result = run_pass_at_k(spec, _run, k=3, require="all")
        # Two clean passes, one cap-truncated trial excluded from the pass count.
        assert result.passes == 2
        assert not result.ok  # require="all" — a non-counted trial fails the gate

    def test_any_fails_when_a_trial_hit_max_turns_even_with_a_clean_pass(self) -> None:
        # In the REAL CI lane (`--trials 3 --require any`), a cap-tainted aggregate
        # must FAIL the gate even though a clean trial passed: a capped trial
        # COULDN'T COMPLETE its work, so it cannot prop up a green gate. The pass
        # COUNT stays diagnostic (one clean pass), but `ok` flips to False.
        spec = _spec()
        it = iter(["success", "max_turns", "max_turns"])

        def _run(_spec: EvalSpec) -> ScenarioResult:
            return _result(spec, passed=True, terminal_reason=next(it))

        result = run_pass_at_k(spec, _run, k=3, require="any")
        assert result.passes == 1  # the one clean trial still counts — diagnostic
        assert result.terminal_reason == "max_turns"
        assert not result.ok  # cap-tainted aggregate fails the require=any gate

    def test_any_fails_on_budget_exceeded_cap_taint(self) -> None:
        spec = _spec()
        it = iter(["success", "budget_exceeded", "max_turns"])

        def _run(_spec: EvalSpec) -> ScenarioResult:
            return _result(spec, passed=True, terminal_reason=next(it))

        result = run_pass_at_k(spec, _run, k=3, require="any")
        assert not result.ok

    def test_any_stays_ok_on_a_clean_matcher_miss_with_no_cap(self) -> None:
        # ANTI-VACUITY GUARD: a clean matcher-miss (terminal_reason NOT in the cap
        # set) is normal pass@k noise — `require="any"` tolerates it. Only a
        # cap-tainted aggregate flips the gate. Revert the cap-taint line and the
        # cap tests above go RED while THIS stays GREEN.
        spec = _spec()
        it = iter([True, False, True])

        def _run(_spec: EvalSpec) -> ScenarioResult:
            return _result(spec, passed=next(it))

        result = run_pass_at_k(spec, _run, k=3, require="any")
        assert result.passes == 2
        assert result.terminal_reason == ""  # the clean miss carries no cap reason
        assert result.ok  # pass@k noise tolerance preserved


class TestRetainsPerTrialResults:
    """The aggregate must retain each trial's ScenarioResult — the transcript evidence.

    The counters are summed/collapsed, but the per-trial transcript report (the
    ``--transcript-html`` artifact) needs the raw per-trial trajectories. Dropping
    them leaves a maintainer with only a pass-count and no way to see WHAT the
    agent did on a failing trial.
    """

    def test_keeps_one_result_per_trial_in_order(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [True, False, True]), k=3, require="any")
        assert len(result.trial_results) == 3
        assert [r.passed for r in result.trial_results] == [True, False, True]

    def test_each_retained_result_carries_its_trial_transcript(self) -> None:
        spec = _spec()

        def _run(_spec: EvalSpec) -> ScenarioResult:
            run = EvalRun(
                spec_name=spec.name,
                tool_calls=(),
                text_blocks=("I will run the command.",),
                terminal_reason="success",
                is_error=False,
                raw_stdout="",
                raw_stderr="",
            )
            return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=False)

        result = run_pass_at_k(spec, _run, k=2, require="any")
        assert all("I will run the command." in r.run.text_blocks for r in result.trial_results)
