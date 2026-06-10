"""Token-usage + fallback threading from the runner onto ``MatrixRow`` (#2192).

A benchmark cell carries the run's :class:`TokenUsage` (summed across trials)
and a ``fell_back`` flag (the cell's billed model differs from the requested
variant's base model). These drive the benchmark's honest cache-cost columns.
The runner is a deterministic fake — never the real SDK — so the threading
contract is pinned without metering.
"""

from pathlib import Path

from teatree.cli.eval.multi_trial import collect_matrix_rows
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


def _run(spec: EvalSpec, *, usage: TokenUsage, billed_model: str | None, cost: float = 0.02) -> EvalRun:
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
    )


class _FixedRunner:
    def __init__(self, *, usage: TokenUsage, billed_model: str | None) -> None:
        self._usage = usage
        self._billed_model = billed_model

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec, usage=self._usage, billed_model=self._billed_model)


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
    def test_not_fell_back_when_billed_matches_requested_base_model(self) -> None:
        runner = _FixedRunner(usage=TokenUsage(input=1), billed_model="claude-opus-4-8")
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8@xhigh"], runner=runner, trials=1, require="any")
        assert _by_key(rows, "alpha", "claude-opus-4-8@xhigh").fell_back is False

    def test_fell_back_when_billed_differs_from_requested_base_model(self) -> None:
        runner = _FixedRunner(usage=TokenUsage(input=1), billed_model="claude-sonnet-4-6")
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8@xhigh"], runner=runner, trials=1, require="any")
        assert _by_key(rows, "alpha", "claude-opus-4-8@xhigh").fell_back is True

    def test_unknown_billed_model_is_not_a_fallback(self) -> None:
        # A subscription/offline run has billed_model=None — never observable as a
        # fallback, so the cell is not marked fell_back.
        runner = _FixedRunner(usage=TokenUsage(), billed_model=None)
        rows = collect_matrix_rows([_spec("alpha")], ["claude-opus-4-8"], runner=runner, trials=1, require="any")
        assert _by_key(rows, "alpha", "claude-opus-4-8").fell_back is False


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
