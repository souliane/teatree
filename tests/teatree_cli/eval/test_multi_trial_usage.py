"""Token-usage + fallback threading from the runner onto ``MatrixRow`` (#2192).

A benchmark cell carries the run's :class:`TokenUsage` (summed across trials)
and a ``fell_back`` flag (the cell's billed model differs from the requested
variant's base model). These drive the benchmark's honest cache-cost columns.
The runner is a deterministic fake — never the real SDK — so the threading
contract is pinned without metering.
"""

from pathlib import Path

from teatree.cli.eval.multi_trial import collect_matrix_rows
from teatree.eval.benchmark import _clean_cost_cells
from teatree.eval.models import EvalRun, EvalSpec, TokenUsage


def _spec(name: str, model: str = "claude-opus-4-8") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
        model=model,
    )


def _run(
    spec: EvalSpec,
    *,
    usage: TokenUsage,
    billed_model: str | None,
    cost: float = 0.02,
    fell_back: bool | None = None,
) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="end_turn",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost,
        usage=usage,
        billed_model=billed_model,
        fell_back=fell_back,
    )


class _FixedRunner:
    def __init__(self, *, usage: TokenUsage, billed_model: str | None, fell_back: bool | None = None) -> None:
        self._usage = usage
        self._billed_model = billed_model
        self._fell_back = fell_back

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec, usage=self._usage, billed_model=self._billed_model, fell_back=self._fell_back)


def _by_key(rows: list, scenario: str, model: str):
    return next(r for r in rows if r.scenario == scenario and r.model == model)


class TestUsageThreading:
    def test_single_trial_carries_run_usage(self) -> None:
        usage = TokenUsage(input=100, cache_creation=200, cache_read=700, output=50)
        runner = _FixedRunner(usage=usage, billed_model="claude-opus-4-8")
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8"], runner=runner, trials=1, require="any")
        (row,) = rows
        assert row.usage == usage

    def test_trials_sum_usage_across_trials(self) -> None:
        usage = TokenUsage(input=10, cache_creation=20, cache_read=70, output=5)
        runner = _FixedRunner(usage=usage, billed_model="claude-opus-4-8")
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8"], runner=runner, trials=3, require="any")
        (row,) = rows
        assert row.usage == TokenUsage(input=30, cache_creation=60, cache_read=210, output=15)


class TestFellBack:
    """``MatrixRow.fell_back`` is sourced from the run's presence signal (#2192).

    The PROVEN false-positive: Claude Code always runs claude-haiku-4-5 as a cheap
    auxiliary, and haiku winning token VOLUME made the old billed-model-by-volume
    derivation flag fell_back on every real run. The run now carries a
    requested-model-presence ``fell_back`` and the cell reads it verbatim.
    """

    def test_run_present_signal_is_not_a_fallback(self) -> None:
        runner = _FixedRunner(usage=TokenUsage(input=1), billed_model="claude-haiku-4-5", fell_back=False)
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8@xhigh"], runner=runner, trials=1, require="any")
        assert _by_key(rows, "alpha", "claude-opus-4-8@xhigh").fell_back is False

    def test_run_absent_signal_is_a_fallback(self) -> None:
        runner = _FixedRunner(usage=TokenUsage(input=1), billed_model="claude-sonnet-4-6", fell_back=True)
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8@xhigh"], runner=runner, trials=1, require="any")
        assert _by_key(rows, "alpha", "claude-opus-4-8@xhigh").fell_back is True

    def test_unobservable_run_signal_is_not_a_fallback(self) -> None:
        # A subscription/offline run has fell_back=None — never observable as a
        # fallback, so the cell is not marked fell_back.
        runner = _FixedRunner(usage=TokenUsage(), billed_model=None, fell_back=None)
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8"], runner=runner, trials=1, require="any")
        assert _by_key(rows, "alpha", "claude-opus-4-8").fell_back is False


def _run_with_reason(spec: EvalSpec, *, reason: str, cost: float = 0.02) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason=reason,
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost,
        usage=TokenUsage(input=10, cache_creation=20, cache_read=70, output=5),
        billed_model="claude-opus-4-8",
    )


class _ReasonRunner:
    """Yields runs whose terminal_reason walks a fixed sequence."""

    def __init__(self, reasons: list[str]) -> None:
        self._reasons = iter(reasons)

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run_with_reason(spec, reason=next(self._reasons))


class TestMultiTrialCapTruncation:
    def test_clean_multi_trial_cell_has_empty_terminal_reason(self) -> None:
        runner = _ReasonRunner(["end_turn", "end_turn", "end_turn"])
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8"], runner=runner, trials=3, require="any")
        (row,) = rows
        assert row.terminal_reason == ""
        # A clean multi-trial cell is metered + not fell-back → it feeds the fit.
        assert len(_clean_cost_cells(rows)) == 1

    def test_capped_trial_marks_multi_trial_cell_and_excludes_it_from_the_fit(self) -> None:
        # One of the 3 trials hit a cap reason. cost/usage are summed across all
        # 3, so the aggregated cell's billed identity is tainted — it must carry a
        # cap terminal_reason and be EXCLUDED from `_clean_cost_cells`. Before the
        # fix the trials>1 branch left terminal_reason="" and the cell leaked into
        # the fit (the exact fabrication this guards against).
        runner = _ReasonRunner(["end_turn", "budget_exceeded", "end_turn"])
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8"], runner=runner, trials=3, require="any")
        (row,) = rows
        assert row.terminal_reason == "budget_exceeded"
        assert _clean_cost_cells(rows) == []


class _AlwaysRaisesRunner:
    def run(self, spec: EvalSpec) -> EvalRun:
        msg = "permanently broken cell"
        raise Exception(msg)  # noqa: TRY002 — mirrors the SDK's bare-Exception transient


class TestErroredCellUsage:
    def test_errored_cell_carries_zero_usage_and_no_fallback(self) -> None:
        rows = collect_matrix_rows(
            [_spec("alpha")], ["claude-opus-4-8"], runner=_AlwaysRaisesRunner(), trials=1, require="any"
        )
        (row,) = rows
        assert row.errored is True
        assert row.usage == TokenUsage()
        assert row.fell_back is False
