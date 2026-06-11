"""API cost aggregation and reporting for metered eval runs.

Anti-vacuous TDD: these tests must go RED when cost aggregation / the summary
line are removed. Proven RED before any implementation landed.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage

from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.report import ScenarioResult, render_json, render_text
from teatree.eval.sdk_runner import SdkInProcessRunner
from teatree.eval.transcript import StreamJsonEvent, extract_cost_usd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_event(cost: float | None = None) -> StreamJsonEvent:
    raw: dict = {"type": "result", "subtype": "success", "is_error": False}
    if cost is not None:
        raw["total_cost_usd"] = cost
    return StreamJsonEvent(line_no=1, type="result", subtype="success", raw=raw)


def _run(*, cost_usd: float = 0.0, spec_name: str = "s") -> EvalRun:
    return EvalRun(
        spec_name=spec_name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost_usd,
    )


def _spec(*, name: str = "s") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="text",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
    )


def _scenario_result(run: EvalRun) -> ScenarioResult:
    return ScenarioResult(
        spec=_spec(name=run.spec_name),
        run=run,
        matcher_results=(),
        skipped=False,
    )


# ---------------------------------------------------------------------------
# extract_cost_usd
# ---------------------------------------------------------------------------


class TestExtractCostUsd:
    def test_returns_cost_from_result_event(self) -> None:
        events = [_result_event(cost=0.05)]
        assert extract_cost_usd(events) == pytest.approx(0.05)

    def test_returns_zero_when_no_result_event(self) -> None:
        assert extract_cost_usd([]) == pytest.approx(0.0)

    def test_returns_zero_when_result_event_has_no_cost_field(self) -> None:
        events = [_result_event(cost=None)]
        assert extract_cost_usd(events) == pytest.approx(0.0)

    def test_uses_last_result_event(self) -> None:
        events = [_result_event(cost=0.01), _result_event(cost=0.09)]
        assert extract_cost_usd(events) == pytest.approx(0.09)

    def test_ignores_non_result_events(self) -> None:
        system_event = StreamJsonEvent(
            line_no=1, type="system", subtype="init", raw={"type": "system", "total_cost_usd": 999}
        )
        events = [system_event, _result_event(cost=0.03)]
        assert extract_cost_usd(events) == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# EvalRun.cost_usd field
# ---------------------------------------------------------------------------


class TestEvalRunCostField:
    def test_default_cost_is_zero(self) -> None:
        run = EvalRun(
            spec_name="x",
            tool_calls=(),
            text_blocks=(),
            terminal_reason="success",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
        )
        assert run.cost_usd == pytest.approx(0.0)

    def test_cost_can_be_set(self) -> None:
        run = _run(cost_usd=0.042)
        assert run.cost_usd == pytest.approx(0.042)


# The in-process SDK runner populates cost_usd from ResultMessage.total_cost_usd


def _sdk_result(total_cost_usd: float | None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s1",
        total_cost_usd=total_cost_usd,
        result="ok",
    )


class TestRunnerCostCapture:
    def _run_with_cost(self, tmp_path: Path, total_cost_usd: float | None) -> EvalRun:
        agent = tmp_path / "agent.md"
        agent.write_text("# fake\nbody\n", encoding="utf-8")
        spec = EvalSpec(
            name="cost_test",
            scenario="test cost",
            agent_path=str(agent),
            prompt="do something",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )

        async def _query(*, prompt, options=None, **_):
            await asyncio.sleep(0)
            yield _sdk_result(total_cost_usd)

        with (
            patch("teatree.eval.sdk_runner.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.eval.sdk_runner.query", _query),
        ):
            return SdkInProcessRunner(workspace=tmp_path).run(spec)

    def test_cost_captured_from_result_event(self, tmp_path: Path) -> None:
        run = self._run_with_cost(tmp_path, 0.01)
        assert run.cost_usd == pytest.approx(0.01)

    def test_cost_zero_when_no_cost_in_result(self, tmp_path: Path) -> None:
        run = self._run_with_cost(tmp_path, None)
        assert run.cost_usd == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# render_text: cost summary line
# ---------------------------------------------------------------------------


class TestRenderTextCostLine:
    def test_metered_run_emits_cost_line(self) -> None:
        results = [_scenario_result(_run(cost_usd=0.01)), _scenario_result(_run(cost_usd=0.02))]
        text = render_text(results)
        assert "API cost: $0.0300 over 2 metered call(s)" in text

    def test_zero_cost_emits_no_metered_calls_line(self) -> None:
        results = [_scenario_result(_run(cost_usd=0.0))]
        text = render_text(results)
        assert "API cost: $0.00 (no metered calls)" in text

    def test_mixed_run_aggregates_only_nonzero(self) -> None:
        results = [
            _scenario_result(_run(cost_usd=0.03)),
            _scenario_result(_run(cost_usd=0.0)),
        ]
        text = render_text(results)
        assert "API cost: $0.0300 over 1 metered call(s)" in text

    def test_cost_line_never_absent(self) -> None:
        results = [_scenario_result(_run(cost_usd=0.0))]
        text = render_text(results)
        assert "API cost:" in text

    def test_anti_vacuous_removal_of_cost_line_makes_test_fail(self) -> None:
        """Verbatim cost line must appear — guards against stripping it from render_text."""
        results = [_scenario_result(_run(cost_usd=0.05))]
        text = render_text(results)
        assert "API cost: $0.0500 over 1 metered call(s)" in text


# ---------------------------------------------------------------------------
# render_json: cost in summary dict
# ---------------------------------------------------------------------------


class TestRenderJsonCost:
    def test_summary_includes_total_cost_usd(self) -> None:
        results = [_scenario_result(_run(cost_usd=0.01)), _scenario_result(_run(cost_usd=0.02))]
        payload = json.loads(render_json(results))
        assert payload["summary"]["total_cost_usd"] == pytest.approx(0.03)
        assert payload["summary"]["metered_calls"] == 2

    def test_summary_zero_cost_for_subscription_runs(self) -> None:
        results = [_scenario_result(_run(cost_usd=0.0))]
        payload = json.loads(render_json(results))
        assert payload["summary"]["total_cost_usd"] == pytest.approx(0.0)
        assert payload["summary"]["metered_calls"] == 0
