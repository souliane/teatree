"""A refused later request keeps the paid first turn and marks coverage incomplete."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.anthropic_api_runner import AnthropicApiRunner
from teatree.eval.backends import ANTHROPIC_API_BACKEND
from teatree.eval.cost_observation import ConservativeSuiteBudget
from teatree.eval.models import EvalSpec, Matcher


class _RefuseSecondRequest(ConservativeSuiteBudget):
    def __init__(self) -> None:
        super().__init__(limit_usd=1000.0)
        self.requests = 0

    def reserve(self, model: str, messages: object, parameters: object, max_output_tokens: int) -> float | None:
        self.requests += 1
        if self.requests == 2:
            self.exhausted = True
            return None
        return super().reserve(model, messages, parameters, max_output_tokens)


def test_cli_keeps_paid_transcript_when_second_request_hits_suite_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = EvalSpec(
        name="partial_paid_run",
        scenario="execute one command",
        agent_path="skills/code/SKILL.md",
        prompt="execute one command",
        model="claude-haiku-4-5",
        tools=("Bash",),
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="paid"),),
        source_path=Path("spec.yaml"),
    )
    turns = 0

    async def stream(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        nonlocal turns
        await asyncio.sleep(0)
        turns += 1
        if turns == 1:
            yield {0: DeltaToolCall(name="Bash", json_args='{"command": "printf paid"}')}
        else:
            yield "done"

    budget = _RefuseSecondRequest()
    runner = AnthropicApiRunner(model=FunctionModel(stream_function=stream), suite_budget=budget)
    monkeypatch.setattr("teatree.cli.eval.app.discover_specs", lambda: [spec])
    monkeypatch.setattr("teatree.cli.eval.app.ensure_django", lambda: None)
    monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", lambda *args, **kwargs: runner)
    summary_json = tmp_path / "summary.json"
    summary_md = tmp_path / "summary.md"
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "run",
            "--backend",
            ANTHROPIC_API_BACKEND,
            "--local",
            "--no-persist",
            "--format",
            "json",
            "--summary-json",
            str(summary_json),
            "--summary-md",
            str(summary_md),
        ],
    )
    assert result.exit_code == 75, result.output
    payload = json.loads(result.stdout)
    assert budget.requests == 2
    assert payload["scenarios"][0]["verdict"] == "incomplete"
    assert payload["scenarios"][0]["tool_calls"][0]["input"]["command"] == "printf paid"
    assert payload["summary"]["total_cost_usd"] > 0
    assert payload["summary"]["failed"] == 0
    summary = json.loads(summary_json.read_text())
    assert summary["scenarios"][0]["verdict"] == "incomplete"
    assert summary["scenarios"][0]["outcome"] == "UNVERIFIED"
    assert "**1 incomplete**" in summary_md.read_text()
