"""``t3 eval run`` persistence + ``t3 eval history`` end-to-end through the CLI.

Integration: a mocked-model eval (the ``claude -p`` boundary is stubbed, never
called) is run, persisted into the ledger, and then read back via
``t3 eval history`` — asserting the recent-runs view, the per-scenario
pass-rate aggregation, and baseline marking. No real model API calls.
"""

import json
from pathlib import Path

import pytest
from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.models import EvalRunRecord, EvalVerdict
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher


def _spec(name: str, *, model: str = "haiku") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario for {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("/tmp/spec.yaml"),
        model=model,
    )


def _run(
    spec_name: str,
    *,
    tool_calls: tuple[EvalToolCall, ...] = (),
    is_error: bool = False,
    cost_usd: float = 0.01,
) -> EvalRun:
    # cost_usd defaults to a non-zero value: sdk runs bill something by
    # definition; the sdk_metered guard fires when total_cost_usd == 0, which
    # would prevent persistence from running.  Stubs represent a metered call.
    return EvalRun(
        spec_name=spec_name,
        tool_calls=tool_calls,
        text_blocks=(),
        terminal_reason="success",
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost_usd,
    )


_PASSING_CALL = (EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),)


def _stub_runner(outcomes: dict[str, EvalRun]) -> type:
    class _StubRunner:
        def __init__(self, *_: object, **__: object) -> None: ...

        def run(self, spec: EvalSpec) -> EvalRun:
            return outcomes[spec.name]

    return _StubRunner


class TestRunPersists(TestCase):
    def _invoke_run(self, specs: list[EvalSpec], outcomes: dict[str, EvalRun], *args: str) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _stub_runner(outcomes)),
        ):
            # The sdk runner is what these persistence tests stub; the default
            # backend is now subscription, so name sdk explicitly. The metered sdk
            # lane defaults to Docker — T3_EVAL_IN_CONTAINER=1 makes it run
            # in-process (the in-container path), so these persistence tests grade
            # the stub runner directly instead of re-routing to a container.
            result = CliRunner().invoke(
                app, ["eval", "run", "--backend", "sdk", *args], env={"T3_EVAL_IN_CONTAINER": "1"}
            )
        assert "Traceback" not in result.output, result.output

    def test_run_records_one_row_per_scenario_with_signals(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        outcomes = {
            "alpha": _run("alpha", tool_calls=_PASSING_CALL),
            "beta": _run("beta"),
        }

        self._invoke_run(specs, outcomes)

        run = EvalRunRecord.objects.latest("started_at")
        assert run.model == "haiku"
        assert run.total == 2
        results = {r.scenario_name: r for r in run.scenario_results.all()}
        assert results["alpha"].verdict == EvalVerdict.PASS
        assert results["alpha"].tool_calls[0]["name"] == "Bash"
        assert results["beta"].verdict == EvalVerdict.FAIL
        assert results["alpha"].matcher_details[0]["passed"] is True

    def test_no_persist_writes_nothing(self) -> None:
        specs = [_spec("alpha")]
        outcomes = {"alpha": _run("alpha", tool_calls=_PASSING_CALL)}

        self._invoke_run(specs, outcomes, "--no-persist")

        assert EvalRunRecord.objects.count() == 0

    def test_baseline_flag_marks_persisted_run(self) -> None:
        specs = [_spec("alpha")]
        outcomes = {"alpha": _run("alpha", tool_calls=_PASSING_CALL)}

        self._invoke_run(specs, outcomes, "--baseline")

        assert EvalRunRecord.objects.latest_baseline() is not None

    def test_mixed_models_recorded_as_joined_model_id(self) -> None:
        specs = [_spec("alpha", model="haiku"), _spec("beta", model="sonnet")]
        outcomes = {
            "alpha": _run("alpha", tool_calls=_PASSING_CALL),
            "beta": _run("beta", tool_calls=_PASSING_CALL),
        }

        self._invoke_run(specs, outcomes)

        assert EvalRunRecord.objects.latest("started_at").model == "haiku,sonnet"


class TestHistoryCommand(TestCase):
    def _record(self, model: str = "haiku", *, is_baseline: bool = False) -> EvalRunRecord:
        run = EvalRunRecord.objects.record(model=model, suite="core", is_baseline=is_baseline)
        run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.PASS)
        run.record_scenario(scenario_name="beta", verdict=EvalVerdict.FAIL)
        return run

    def test_history_shows_recent_runs_and_pass_rates(self) -> None:
        self._record()
        result = CliRunner().invoke(app, ["eval", "history"])
        assert result.exit_code == 0
        assert "alpha: 1/1" in result.output
        assert "beta: 0/1" in result.output

    def test_history_empty_message_when_no_runs(self) -> None:
        result = CliRunner().invoke(app, ["eval", "history"])
        assert result.exit_code == 0
        assert "(no eval runs recorded)" in result.output

    def test_history_json_carries_pass_rate(self) -> None:
        self._record()
        result = CliRunner().invoke(app, ["eval", "history", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output[result.output.index("{") : result.output.rindex("}") + 1])
        rates = {r["scenario"]: r for r in payload["runs"][0]["pass_rates"]}
        assert rates["alpha"]["pass_rate"] == pytest.approx(1.0)
        assert rates["beta"]["pass_rate"] == pytest.approx(0.0)

    def test_history_model_filter_scopes_runs(self) -> None:
        self._record(model="haiku")
        self._record(model="sonnet")
        result = CliRunner().invoke(app, ["eval", "history", "--model", "sonnet"])
        assert "model=sonnet" in result.output
        assert "model=haiku" not in result.output

    def test_history_baseline_filter_shows_only_baseline(self) -> None:
        self._record(model="haiku", is_baseline=True)
        self._record(model="haiku", is_baseline=False)
        result = CliRunner().invoke(app, ["eval", "history", "--baseline"])
        assert result.output.count("[baseline]") == 1

    def test_mark_baseline_promotes_run(self) -> None:
        run = self._record()
        result = CliRunner().invoke(app, ["eval", "history", "--mark-baseline", str(run.pk)])
        assert result.exit_code == 0
        run.refresh_from_db()
        assert run.is_baseline is True

    def test_mark_baseline_unknown_id_exits_2(self) -> None:
        result = CliRunner().invoke(app, ["eval", "history", "--mark-baseline", "9999"])
        assert result.exit_code == 2
        assert "unknown run id" in result.output

    def test_history_unknown_format_exits_2(self) -> None:
        result = CliRunner().invoke(app, ["eval", "history", "--format", "yaml"])
        assert result.exit_code == 2
        assert "unknown --format" in result.output
