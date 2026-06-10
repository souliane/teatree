"""``t3 eval list`` / ``t3 eval run`` end-to-end through the typer CLI."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.corpus import CorpusGradeRow
from teatree.cli.eval.docker import DockerUnavailableError
from teatree.eval.coverage import CoverageReport, SkillCoverage
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.negative_control import NegativeControlOutcome
from teatree.eval.persistence import persist_run
from teatree.eval.regression_corpus import CheckResult, RegressionCheck, RegressionReport
from teatree.eval.report import MatcherResult, ScenarioResult, evaluate
from teatree.eval.skill_command_validity import CommandValidityReport, CommandViolation
from teatree.eval.skill_prose_judge import ProseJudgeReport, ProseScore
from teatree.eval.transcript_conformance import InvariantResult
from teatree.eval.trigger_qa import TriggerCheck, TriggerQAReport

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _stub_oauth_token() -> "Iterator[None]":
    """Stop ``make_runner("sdk")`` from shelling ``pass``/``gpg`` for the token.

    Every ``--backend sdk`` CLI test builds the runner through
    ``teatree.eval.backends.make_runner``, which calls ``ensure_oauth_token()``
    → ``read_pass("anthropic/oauth-token")`` → a ``pass`` subprocess that blocks
    on ``gpg`` on a dev machine without ``CLAUDE_CODE_OAUTH_TOKEN`` set. The stub
    keeps the suite hermetic — it never touches the host's secret store.

    It also sets ``T3_EVAL_IN_CONTAINER=1`` so the metered ``--backend sdk`` /
    ``--trials`` / ``--models`` runs execute IN-PROCESS (Docker is the default for
    the metered lane; the marker is exactly what the docker runner sets inside the
    container to run the re-invoked command in-process — the faithful test of the
    in-container behaviour). Tests that assert the docker-routing path itself
    override the env explicitly.
    """
    with (
        patch("teatree.eval.backends.ensure_oauth_token", return_value="t"),
        patch.dict("os.environ", {"T3_EVAL_IN_CONTAINER": "1"}),
    ):
        yield


def _spec(name: str = "scenario_a") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario for {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("/tmp/spec.yaml"),
    )


def _run(
    spec_name: str,
    *,
    terminal_reason: str = "success",
    is_error: bool = False,
    tool_calls: tuple[EvalToolCall, ...] = (),
    cost_usd: float = 0.05,
) -> EvalRun:
    return EvalRun(
        spec_name=spec_name,
        tool_calls=tool_calls,
        text_blocks=(),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost_usd,
    )


_PASSING_CALL = (EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),)


class TestEvalList:
    def test_lists_discovered_scenario_names(self) -> None:
        with patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha"), _spec("beta")]):
            result = CliRunner().invoke(app, ["eval", "list"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_renders_rich_table_with_box_and_headers(self) -> None:
        with patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha")]):
            result = CliRunner().invoke(app, ["eval", "list"])
        assert result.exit_code == 0
        assert any(ch in result.output for ch in "─│┌┐└┘╭╮╰╯"), result.output
        for header in ("Name", "Scenario", "Agent", "File", "Asserts"):
            assert header in result.output, f"missing header {header!r}: {result.output}"

    def test_table_shows_matcher_count_and_source_filename(self) -> None:
        spec = _spec("gamma")
        with patch("teatree.cli.eval.app.discover_specs", return_value=[spec]):
            result = CliRunner().invoke(app, ["eval", "list"])
        assert result.exit_code == 0
        assert "gamma" in result.output
        assert str(len(spec.matchers)) in result.output
        assert spec.source_path.name in result.output

    def test_prints_placeholder_when_no_scenarios(self) -> None:
        with patch("teatree.cli.eval.app.discover_specs", return_value=[]):
            result = CliRunner().invoke(app, ["eval", "list"])
        assert result.exit_code == 0
        assert "(no scenarios discovered)" in result.output


class TestEvalRun:
    def test_sdk_run_default_budget_is_generous_so_a_scenario_completes(self) -> None:
        # The metered `t3 eval run --backend sdk` default budget is GENEROUS, not
        # the cheap 0.10 floor: a truncated run measures the cap, not behaviour
        # (the first full metered run lost scenarios to budget_exceeded). The flag
        # still threads a per-run override.
        from teatree.cli.eval.app import METERED_DEFAULT_BUDGET_USD  # noqa: PLC0415

        _BudgetCapturingRunner.last_max_budget_usd = None
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _BudgetCapturingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert _BudgetCapturingRunner.last_max_budget_usd == pytest.approx(METERED_DEFAULT_BUDGET_USD)
        assert METERED_DEFAULT_BUDGET_USD > 0.10

    def test_effort_flag_threads_to_the_runner(self) -> None:
        # `--effort high` reaches the SdkInProcessRunner's effort kwarg so the
        # metered lane runs at a representative reasoning effort.
        _EffortCapturingRunner.last_effort = "unset"
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _EffortCapturingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--effort", "high", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert _EffortCapturingRunner.last_effort == "high"

    def test_effort_defaults_to_a_representative_level(self) -> None:
        # The metered lane defaults to a representative effort (not the model's
        # default) so the measured pass-rate reflects real high-effort usage.
        from teatree.cli.eval.app import METERED_DEFAULT_EFFORT  # noqa: PLC0415

        _EffortCapturingRunner.last_effort = "unset"
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _EffortCapturingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert _EffortCapturingRunner.last_effort == METERED_DEFAULT_EFFORT
        assert METERED_DEFAULT_EFFORT == "high"

    def test_sdk_run_max_budget_usd_flag_threads_to_the_runner(self) -> None:
        _BudgetCapturingRunner.last_max_budget_usd = None
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _BudgetCapturingRunner),
        ):
            result = CliRunner().invoke(
                app, ["eval", "run", "--backend", "sdk", "--max-budget-usd", "1.5", "--no-persist"]
            )
        assert result.exit_code == 0, result.output
        assert _BudgetCapturingRunner.last_max_budget_usd == pytest.approx(1.5)

    def test_runs_all_scenarios_when_no_name(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS alpha" in result.output
        assert "PASS beta" in result.output

    def test_parallel_flag_is_forwarded_to_run_specs(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        captured: dict[str, int] = {}

        def _fake_run_specs(runner: object, run_specs_arg: list[EvalSpec], *, parallel: int) -> list[EvalRun]:
            captured["parallel"] = parallel
            return [_run(s.name, tool_calls=_PASSING_CALL) for s in run_specs_arg]

        class _StubRunner:
            def __init__(self, *_: object, **__: object) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
            patch("teatree.cli.eval.app.run_specs", side_effect=_fake_run_specs),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist", "--parallel", "8"])
        assert result.exit_code == 0, result.output
        assert captured["parallel"] == 8

    def test_runs_one_scenario_when_name_given(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.app.find_spec", return_value=specs[0]),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "alpha", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "PASS beta" not in result.output

    def test_unknown_scenario_exits_with_code_2_and_lists_available(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.app.find_spec", return_value=None),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "missing"])
        assert result.exit_code == 2
        assert "unknown scenario: 'missing'" in result.output
        assert "available scenarios: alpha, beta" in result.output

    def test_unknown_scenario_shows_none_when_empty_inventory(self) -> None:
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=[]),
            patch("teatree.cli.eval.app.find_spec", return_value=None),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "missing"])
        assert result.exit_code == 2
        assert "available scenarios: (none)" in result.output


class TestTranscriptReplay:
    def test_non_utf8_transcript_does_not_crash(self, tmp_path: Path) -> None:
        # #1652: a session JSONL carrying non-UTF-8 bytes must replay what it
        # can (errors="replace") instead of raising UnicodeDecodeError. The
        # valid lines from the all_pass fixture still parse green.
        fixture = Path(__file__).parents[1] / "fixtures" / "transcripts" / "all_pass.session.jsonl"
        transcript = tmp_path / "session.jsonl"
        transcript.write_bytes(fixture.read_bytes() + b'\n{"type":"user","message":"\xff\xfe bad bytes"}\n')

        result = CliRunner().invoke(app, ["eval", "transcript-replay", "--file", str(transcript)])

        assert "UnicodeDecodeError" not in result.output
        assert result.exit_code == 0, result.output

    def test_unknown_format_exits_with_code_2(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--format", "yaml"])
        assert result.exit_code == 2
        assert "unknown --format" in result.output

    def test_json_format_emits_parseable_json(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--format", "json", "--no-persist"])
        assert result.exit_code == 0
        # Other pytest plugins (e.g. inline-snapshot) can write banners to
        # stdout during the test session; isolate the JSON document by
        # slicing from the first '{' to the last '}'.
        output = result.output
        start = output.index("{")
        end = output.rindex("}") + 1
        payload = json.loads(output[start:end])
        assert payload["summary"]["total"] == 1

    def test_html_format_emits_self_contained_document(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--format", "html", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "<!doctype html>" in result.output.lower()
        assert "alpha" in result.output

    def test_html_format_rejected_on_history_command(self) -> None:
        result = CliRunner().invoke(app, ["eval", "history", "--format", "html"])
        assert result.exit_code == 2
        assert "unknown --format" in result.output

    def test_exits_nonzero_when_any_scenario_failed(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                # No tool_calls → the positive matcher fails.
                return _run(spec.name)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 1
        assert "FAIL alpha" in result.output

    def test_max_turns_override_is_passed_to_runner(self) -> None:
        specs = [_spec("alpha")]
        captured: dict[str, object] = {}

        class _StubRunner:
            def __init__(
                self, *, max_turns_override: int | None = None, require_executed: bool = False, **_: object
            ) -> None:
                captured["max_turns_override"] = max_turns_override
                captured["require_executed"] = require_executed

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--max-turns", "9", "--no-persist"])
        assert result.exit_code == 0
        assert captured["max_turns_override"] == 9

    def test_sdk_backend_forces_require_executed_without_the_flag(self) -> None:
        # "if we run, of course we want it executed" — the sdk path arms the
        # all-skipped gate unconditionally; --require-executed is not opt-in for it.
        specs = [_spec("alpha")]
        captured: dict[str, object] = {}

        class _StubRunner:
            def __init__(
                self, *, max_turns_override: int | None = None, require_executed: bool = False, **_: object
            ) -> None:
                captured["require_executed"] = require_executed

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL, cost_usd=0.05)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert captured["require_executed"] is True

    def test_executed_but_unmetered_sdk_run_fails_loud(self) -> None:
        # The $0/no-metered-calls state (the --bare auth bug) must FAIL, never pass.
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                # Executed (not skipped), matchers pass, but $0 metered → the
                # vacuous-green state. The guard must turn this RED.
                return _run(spec.name, tool_calls=_PASSING_CALL, cost_usd=0.0)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 1, result.output
        assert "metered" in result.output.lower()

    def test_metered_sdk_run_passes(self) -> None:
        # The same passing run WITH real metered cost stays green — proves the
        # guard keys on cost, not on the verdict (anti-vacuous companion).
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL, cost_usd=0.0556)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0, result.output


class TestEvalPassAtK:
    def test_trials_aggregates_and_reports_pass_rate(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "3", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS alpha (3/3 trials" in result.output

    def test_require_all_fails_when_a_trial_fails(self) -> None:
        specs = [_spec("alpha")]
        calls = {"n": 0}

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                calls["n"] += 1
                return _run(spec.name, tool_calls=_PASSING_CALL if calls["n"] == 1 else ())

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--require", "all", "--no-persist"])
        assert result.exit_code == 1
        assert "FAIL alpha (1/2 trials" in result.output

    def test_bad_require_exits_code_2(self) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--require", "most", "--no-persist"])
        assert result.exit_code == 2
        assert "unknown --require" in result.output

    def test_json_format_reports_pass_rate(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--format", "json", "--no-persist"])
        assert result.exit_code == 0, result.output
        output = result.output
        payload = json.loads(output[output.index("{") : output.rindex("}") + 1])
        assert payload["mode"] == "pass@2"
        assert payload["scenarios"][0]["passes"] == 2

    def test_all_trials_skipped_reports_skip_line_but_fails_loud(self) -> None:
        # The per-scenario SKIP line is still printed for visibility, but because
        # --trials always runs the metered sdk runner, an all-skipped run can only
        # mean claude/credential is unprovisioned — it fails loud, never green.
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, terminal_reason="skipped: claude binary not on PATH")

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--no-persist"])
        assert result.exit_code != 0, result.output
        assert "SKIP alpha" in result.output
        assert "executed 0" in result.output


class _SkippingRunner:
    def __init__(self, *_, **__) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name, terminal_reason="skipped: claude binary not on PATH")


class TestEvalRequireExecuted:
    """`--require-executed` turns a decorative (collected>0, ran==0) run red."""

    def test_single_trial_all_skipped_exits_nonzero_when_required(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--require-executed", "--no-persist"])
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_single_trial_sdk_all_skipped_fails_loud_without_flag(self) -> None:
        # The sdk backend IS the metered path: "if we run, of course we want it
        # executed". An all-skipped sdk run fails loud even without the flag.
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_single_trial_subscription_all_skipped_stays_green_without_flag(self, tmp_path: Path) -> None:
        # The subscription backend's pre-transcript all-skip is legitimate and
        # stays green — the flag is still opt-in there.
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--no-persist", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output

    def test_single_trial_with_execution_passes_under_flag(self) -> None:
        specs = [_spec("alpha")]

        class _PassingRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _PassingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--require-executed", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS alpha" in result.output

    def test_pass_at_k_all_skipped_exits_nonzero_when_required(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "3", "--require-executed", "--no-persist"])
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_pass_at_k_all_skipped_fails_loud_without_flag(self) -> None:
        # --trials always uses the metered sdk runner, so an all-skipped pass@k
        # run fails loud even without the flag (it can never be a legit all-skip).
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "3", "--no-persist"])
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_pass_at_k_with_execution_passes_under_flag(self) -> None:
        specs = [_spec("alpha")]

        class _PassingRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _PassingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "3", "--require-executed", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS alpha" in result.output

    def test_zero_collected_stays_green_under_flag(self) -> None:
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=[]),
            patch("teatree.eval.backends.SdkInProcessRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--require-executed", "--no-persist"])
        assert result.exit_code == 0, result.output


class TestEvalSkillTriggers:
    def test_shipped_corpus_passes(self) -> None:
        result = CliRunner().invoke(app, ["eval", "skill-triggers"])
        assert result.exit_code == 0, result.output
        assert "0 failed" in result.output

    def test_reports_failure_and_exits_nonzero(self) -> None:
        bad = TriggerQAReport(checks=(TriggerCheck("debug", "no scope here", should_fire=True, fired=False),))
        with patch("teatree.cli.eval.lanes.run_trigger_qa", return_value=bad):
            result = CliRunner().invoke(app, ["eval", "skill-triggers"])
        assert result.exit_code == 1
        assert "under-trigger" in result.output

    def test_json_format_emits_checks(self) -> None:
        good = TriggerQAReport(checks=(TriggerCheck("debug", "the build is broken", should_fire=True, fired=True),))
        with patch("teatree.cli.eval.lanes.run_trigger_qa", return_value=good):
            result = CliRunner().invoke(app, ["eval", "skill-triggers", "--format", "json"])
        assert result.exit_code == 0
        output = result.output
        payload = json.loads(output[output.index("{") : output.rindex("}") + 1])
        assert payload["ok"] is True
        assert payload["checks"][0]["skill"] == "debug"

    def test_over_trigger_message_for_unexpected_fire(self) -> None:
        bad = TriggerQAReport(checks=(TriggerCheck("debug", "open a PR", should_fire=False, fired=True),))
        with patch("teatree.cli.eval.lanes.run_trigger_qa", return_value=bad):
            result = CliRunner().invoke(app, ["eval", "skill-triggers"])
        assert result.exit_code == 1
        assert "over-trigger" in result.output

    def test_old_trigger_qa_command_is_gone(self) -> None:
        result = CliRunner().invoke(app, ["eval", "trigger-qa"])
        assert result.exit_code != 0, result.output


class _PassRunner:
    def __init__(self, *_: object, **__: object) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name, tool_calls=_PASSING_CALL)


class TestEvalBackend:
    def test_unknown_backend_exits_with_code_2(self) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "magic", "--no-persist"])
        assert result.exit_code == 2
        assert "unknown eval backend" in result.output

    def test_subscription_backend_grades_a_saved_transcript(self, tmp_path: Path) -> None:
        specs = [_spec("worktree_first")]
        transcript = (Path(__file__).parents[1] / "eval" / "fixtures" / "worktree_first_pass.stream.jsonl").read_text(
            encoding="utf-8"
        )
        (tmp_path / "worktree_first.jsonl").write_text(transcript, encoding="utf-8")

        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(
                app,
                ["eval", "run", "--backend", "subscription", "--transcript-dir", str(tmp_path), "--no-persist"],
            )
        assert result.exit_code == 0, result.output
        assert "PASS worktree_first" in result.output

    def test_default_backend_is_subscription(self, tmp_path: Path) -> None:
        # A bare `t3 eval run` must NOT shell the metered sdk runner: it grades a
        # saved subscription transcript. Pointed at a transcript dir holding one,
        # the scenario passes without `--backend` ever being given.
        specs = [_spec("worktree_first")]
        transcript = (Path(__file__).parents[1] / "eval" / "fixtures" / "worktree_first_pass.stream.jsonl").read_text(
            encoding="utf-8"
        )
        (tmp_path / "worktree_first.jsonl").write_text(transcript, encoding="utf-8")

        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--transcript-dir", str(tmp_path), "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS worktree_first" in result.output

    def test_default_backend_missing_transcript_prints_clear_hint(self, tmp_path: Path) -> None:
        # The missing-transcript UX: a bare run with no transcripts skips cleanly
        # (exit 0) and names the scenario, the expected path, and the recipe to
        # produce it — never a silent no-op.
        specs = [_spec("worktree_first")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--transcript-dir", str(tmp_path), "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "SKIP worktree_first" in result.output
        assert str(tmp_path / "worktree_first.jsonl") in result.output
        assert "prepare-subscription" in result.output

    def test_trials_with_default_backend_warns_metered(self, tmp_path: Path) -> None:
        # `--trials` forces the metered sdk runner even under the subscription
        # default; the user is told so.
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _PassRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "metered in-process Agent-SDK runner" in result.output


@pytest.mark.django_db
class TestEvalPersistAndHistory:
    def test_persists_and_history_lists_it(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value="sha123"),
        ):
            run_result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk"])
            assert run_result.exit_code == 0, run_result.output
            history_result = CliRunner().invoke(app, ["eval", "history"])
        assert history_result.exit_code == 0, history_result.output
        assert "1 passed" in history_result.output

    def test_history_empty_when_nothing_recorded(self) -> None:
        result = CliRunner().invoke(app, ["eval", "history"])
        assert result.exit_code == 0
        assert "(no eval runs recorded)" in result.output

    def test_history_json_shape(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            CliRunner().invoke(app, ["eval", "run", "--backend", "sdk"])
            result = CliRunner().invoke(app, ["eval", "history", "--format", "json"])
        payload = json.loads(result.output[result.output.index("{") : result.output.rindex("}") + 1])
        assert payload["runs"][0]["passed"] == 1
        assert payload["runs"][0]["model"] == "claude-sonnet-4-6"

    def test_mark_baseline_promotes_run(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--baseline"])
            result = CliRunner().invoke(app, ["eval", "history", "--baseline"])
        assert result.exit_code == 0, result.output
        assert "[baseline]" in result.output

    def test_gate_regressions_flags_drop_against_baseline(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            first = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--baseline"])
            assert first.exit_code == 0, first.output

        class _FailRunner:
            def __init__(self, *_: object, **__: object) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name)

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _FailRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            second = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--gate-regressions"])

        assert second.exit_code == 1, second.output
        assert "REGRESSED alpha" in second.output


def _cost_runner(cost_usd: float) -> type:
    class _CostRunner:
        def __init__(self, *_: object, **__: object) -> None: ...

        def run(self, spec: EvalSpec) -> EvalRun:
            return _run(spec.name, tool_calls=_PASSING_CALL, cost_usd=cost_usd)

    return _CostRunner


@pytest.mark.django_db
class TestEvalCostRegressionGate:
    def _record_baseline(self, specs: list[EvalSpec], *, cost_usd: float) -> None:
        # A zero-cost baseline is a subscription/free run (no metered cost) — persist it
        # through the ledger directly, the way such a baseline really lands, rather than
        # the metered sdk path (whose $0-fail guard would correctly reject it).
        results = [evaluate(spec, _run(spec.name, tool_calls=_PASSING_CALL, cost_usd=cost_usd)) for spec in specs]
        record = persist_run(results, model="claude-sonnet-4-6", git_sha="")
        record.mark_baseline()

    def _run_candidate(self, specs: list[EvalSpec], *, cost_usd: float, extra: list[str]) -> object:
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _cost_runner(cost_usd)),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            return CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", *extra])

    def test_cost_spike_beyond_tolerance_exits_non_zero(self) -> None:
        specs = [_spec("alpha")]
        self._record_baseline(specs, cost_usd=0.10)

        result = self._run_candidate(specs, cost_usd=0.30, extra=["--gate-cost-regression"])

        assert result.exit_code == 1, result.output
        assert "COST REGRESSED alpha" in result.output

    def test_cost_within_tolerance_passes(self) -> None:
        specs = [_spec("alpha")]
        self._record_baseline(specs, cost_usd=0.10)

        result = self._run_candidate(specs, cost_usd=0.11, extra=["--gate-cost-regression"])

        assert result.exit_code == 0, result.output
        assert "COST REGRESSED" not in result.output

    def test_explicit_tolerance_flag_raises_the_bar(self) -> None:
        specs = [_spec("alpha")]
        self._record_baseline(specs, cost_usd=0.10)

        result = self._run_candidate(
            specs, cost_usd=0.30, extra=["--gate-cost-regression", "--cost-regression-tolerance", "3.0"]
        )

        assert result.exit_code == 0, result.output
        assert "COST REGRESSED" not in result.output

    def test_zero_baseline_cost_passes_without_div_by_zero(self) -> None:
        # The baseline run EXISTS but its per-scenario cost is $0 (a subscription/free
        # baseline). The relative drift is undefined, so the gate skips the scenario:
        # exit 0, never a COST REGRESSED, never a divide-by-zero — and NOT the
        # "no cost baseline" path (a baseline run is present, just zero-cost).
        specs = [_spec("alpha")]
        self._record_baseline(specs, cost_usd=0.0)

        result = self._run_candidate(specs, cost_usd=0.50, extra=["--gate-cost-regression"])

        assert result.exit_code == 0, result.output
        assert "COST REGRESSED" not in result.output

    def test_no_baseline_recorded_reports_and_passes(self) -> None:
        specs = [_spec("alpha")]

        result = self._run_candidate(specs, cost_usd=0.50, extra=["--gate-cost-regression"])

        assert result.exit_code == 0, result.output
        assert "no cost baseline" in result.output


@pytest.mark.django_db
class TestEvalModelMatrix:
    def test_matrix_runs_each_model_and_renders_columns(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _PassRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus,haiku", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "opus" in result.output
        assert "haiku" in result.output
        assert "alpha" in result.output
        assert "opus: 2 passed" in result.output

    def test_matrix_json_shape(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _PassRunner),
        ):
            result = CliRunner().invoke(
                app, ["eval", "run", "--models", "opus,haiku", "--format", "json", "--no-persist"]
            )
        payload = json.loads(result.output[result.output.index("{") : result.output.rindex("}") + 1])
        assert payload["models"] == ["opus", "haiku"]
        assert payload["scenarios"][0]["results"]["opus"]["passed"] is True

    def test_matrix_exits_nonzero_on_failure(self) -> None:
        specs = [_spec("alpha")]

        class _FailOnHaiku:
            def __init__(self, *_: object, **__: object) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL if spec.model == "opus" else ())

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _FailOnHaiku),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus,haiku", "--no-persist"])
        assert result.exit_code == 1, result.output
        assert "opus: 1 passed" in result.output
        assert "haiku: 0 passed, 1 failed" in result.output

    def test_empty_models_exits_code_2(self) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--models", " , ", "--no-persist"])
        assert result.exit_code == 2
        assert "--models was empty" in result.output

    def test_matrix_persists_each_model(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            CliRunner().invoke(app, ["eval", "run", "--models", "opus,haiku"])
            history = CliRunner().invoke(app, ["eval", "history", "--format", "json"])
        payload = json.loads(history.output[history.output.index("{") : history.output.rindex("}") + 1])
        assert payload["runs"][0]["model"] == "opus,haiku"

    def test_matrix_all_skipped_exits_nonzero_when_required(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(
                app, ["eval", "run", "--models", "opus,haiku", "--require-executed", "--no-persist"]
            )
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_matrix_all_skipped_fails_loud_without_flag(self) -> None:
        # --models always uses the metered sdk runner, so an all-skipped matrix
        # run fails loud even without the flag.
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus,haiku", "--no-persist"])
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output


@pytest.mark.django_db
class TestEvalModelVariantMatrix:
    """`--models` accepts `model@effort` variants; the tag is the identity string."""

    def test_variant_tags_render_as_matrix_columns(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _PassRunner),
        ):
            result = CliRunner().invoke(
                app,
                ["eval", "run", "--models", "claude-opus-4-8@xhigh,claude-fable-5@medium", "--no-persist"],
            )
        assert result.exit_code == 0, result.output
        assert "claude-opus-4-8@xhigh" in result.output
        assert "claude-fable-5@medium" in result.output

    def test_variant_tag_is_persisted_as_the_model_identity(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            CliRunner().invoke(app, ["eval", "run", "--models", "opus@xhigh,opus@medium"])
            history = CliRunner().invoke(app, ["eval", "history", "--format", "json"])
        payload = json.loads(history.output[history.output.index("{") : history.output.rindex("}") + 1])
        assert payload["runs"][0]["model"] == "opus@xhigh,opus@medium"

    def test_unknown_effort_exits_code_2_with_known_levels(self) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus@turbo", "--no-persist"])
        assert result.exit_code == 2
        assert "unknown effort 'turbo'" in result.output
        assert "xhigh" in result.output

    def test_html_format_is_rejected_for_a_matrix_run(self) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus", "--format", "html", "--no-persist"])
        assert result.exit_code == 2
        assert "only supported for a single-trial run" in result.output


class _BenchmarkRunner:
    """Passes everything on `@xhigh` variants; fails `beta` elsewhere; costed per call."""

    def __init__(self, *_: object, **__: object) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        passing = spec.model.endswith("@xhigh") or spec.name == "alpha"
        cost = 0.20 if spec.model.endswith("@xhigh") else 0.05
        return _run(spec.name, tool_calls=_PASSING_CALL if passing else (), cost_usd=cost)


class _BudgetCapturingRunner:
    """Records the ``max_budget_usd`` it was constructed with; passes every scenario."""

    last_max_budget_usd: float | None = None

    def __init__(self, *_: object, max_budget_usd: float, **__: object) -> None:
        type(self).last_max_budget_usd = max_budget_usd

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name, tool_calls=_PASSING_CALL, cost_usd=0.20)


class _EffortCapturingRunner:
    """Records the ``effort`` it was constructed with; passes every scenario."""

    last_effort: object = "unset"

    def __init__(self, *_: object, effort: object = None, **__: object) -> None:
        type(self).last_effort = effort

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name, tool_calls=_PASSING_CALL, cost_usd=0.20)


@pytest.mark.django_db
class TestEvalBenchmark:
    def _invoke(self, args: list[str], *, specs: list[EvalSpec], runner: type = _BenchmarkRunner):
        # The marker emulates running INSIDE the CI container, where the benchmark
        # runs in-process (Docker is the default; the marker breaks the re-route).
        with (
            patch("teatree.cli.eval.benchmark.discover_specs", return_value=specs),
            patch("teatree.cli.eval.benchmark.SdkInProcessRunner", runner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            return CliRunner().invoke(app, ["eval", "benchmark", *args], env={"T3_EVAL_IN_CONTAINER": "1"})

    def test_renders_per_variant_comparison_table(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        result = self._invoke(["--models", "claude-opus-4-8@xhigh,claude-fable-5@medium", "--no-persist"], specs=specs)
        assert result.exit_code == 0, result.output
        assert "claude-opus-4-8@xhigh" in result.output
        assert "claude-fable-5@medium" in result.output
        assert "2/2" in result.output
        assert "1/2" in result.output

    def test_trials_aggregate_cost_and_pass_rate_per_cell(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        result = self._invoke(
            ["--models", "opus@xhigh", "--trials", "2", "--format", "json", "--no-persist"], specs=specs
        )
        assert result.exit_code == 0, result.output
        (entry,) = json.loads(result.output)["variants"]
        assert (entry["passed"], entry["executed"]) == (2, 2)
        assert entry["total_cost_usd"] == pytest.approx(0.80)

    def test_json_shape_carries_the_comparison_metrics(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        result = self._invoke(["--models", "opus@xhigh,fable@medium", "--format", "json", "--no-persist"], specs=specs)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        by_variant = {entry["variant"]: entry for entry in payload["variants"]}
        opus = by_variant["opus@xhigh"]
        assert (opus["passed"], opus["executed"]) == (2, 2)
        assert opus["pass_rate"] == pytest.approx(1.0)
        assert opus["total_cost_usd"] == pytest.approx(0.40)
        assert opus["cost_per_pass_usd"] == pytest.approx(0.20)
        fable = by_variant["fable@medium"]
        assert (fable["passed"], fable["executed"]) == (1, 2)
        assert fable["cost_per_pass_usd"] == pytest.approx(0.10)

    def test_failing_scenarios_are_data_not_an_exit_failure(self) -> None:
        # The benchmark is a comparison report, not a gate: a weaker variant
        # failing scenarios is the measurement, never a non-zero exit.
        specs = [_spec("alpha"), _spec("beta")]
        result = self._invoke(["--models", "fable@medium", "--no-persist"], specs=specs)
        assert result.exit_code == 0, result.output

    def test_scenarios_flag_filters_the_suite(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        result = self._invoke(
            ["--models", "opus@xhigh", "--scenarios", "alpha", "--format", "json", "--no-persist"],
            specs=specs,
        )
        assert result.exit_code == 0, result.output
        (entry,) = json.loads(result.output)["variants"]
        assert entry["executed"] == 1

    def test_default_budget_is_generous_so_a_scenario_completes(self) -> None:
        # The benchmark default must be high enough that an opus@xhigh scenario
        # finishes rather than truncates — a truncated run is a false measurement.
        _BudgetCapturingRunner.last_max_budget_usd = None
        specs = [_spec("alpha")]
        result = self._invoke(["--models", "opus@xhigh", "--no-persist"], specs=specs, runner=_BudgetCapturingRunner)
        assert result.exit_code == 0, result.output
        assert _BudgetCapturingRunner.last_max_budget_usd == pytest.approx(2.0)

    def test_max_budget_usd_flag_threads_to_the_runner(self) -> None:
        _BudgetCapturingRunner.last_max_budget_usd = None
        specs = [_spec("alpha")]
        result = self._invoke(
            ["--models", "opus@xhigh", "--max-budget-usd", "1.5", "--no-persist"],
            specs=specs,
            runner=_BudgetCapturingRunner,
        )
        assert result.exit_code == 0, result.output
        assert _BudgetCapturingRunner.last_max_budget_usd == pytest.approx(1.5)

    def test_unknown_scenario_exits_code_2(self) -> None:
        specs = [_spec("alpha")]
        result = self._invoke(["--models", "opus@xhigh", "--scenarios", "nope", "--no-persist"], specs=specs)
        assert result.exit_code == 2
        assert "unknown scenario" in result.output

    def test_unknown_effort_exits_code_2(self) -> None:
        specs = [_spec("alpha")]
        result = self._invoke(["--models", "opus@turbo", "--no-persist"], specs=specs)
        assert result.exit_code == 2
        assert "unknown effort 'turbo'" in result.output

    def test_empty_models_exits_code_2(self) -> None:
        specs = [_spec("alpha")]
        result = self._invoke(["--models", " , ", "--no-persist"], specs=specs)
        assert result.exit_code == 2
        assert "--models was empty" in result.output

    def test_all_skipped_fails_loud(self) -> None:
        # Benchmark is metered (`--backend sdk` semantics): the all-skipped
        # require-executed gate is always armed, never a decorative green.
        specs = [_spec("alpha")]
        result = self._invoke(["--models", "opus@xhigh", "--no-persist"], specs=specs, runner=_SkippingRunner)
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_persists_one_matrix_record_with_variant_tags(self) -> None:
        specs = [_spec("alpha")]
        self._invoke(["--models", "opus@xhigh,fable@medium"], specs=specs)
        history = CliRunner().invoke(app, ["eval", "history", "--format", "json"])
        payload = json.loads(history.output[history.output.index("{") : history.output.rindex("}") + 1])
        assert payload["runs"][0]["model"] == "opus@xhigh,fable@medium"


class TestBenchmarkDockerByDefault:
    """``t3 eval benchmark`` is metered → it defaults to running IN the container.

    The module-wide autouse fixture sets ``T3_EVAL_IN_CONTAINER=1``; these tests
    clear it (``patch.dict(..., clear=True)``) to exercise the HOST-side routing
    decision (whether to spawn docker), then re-route or not as the case under
    test demands.
    """

    def test_default_routes_to_docker_and_threads_args(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.cli.eval.benchmark.run_eval_in_docker", return_value=0) as docker,
        ):
            result = CliRunner().invoke(
                app,
                [
                    "eval",
                    "benchmark",
                    "--models",
                    "claude-opus-4-8@xhigh,claude-fable-5@medium",
                    "--scenarios",
                    "alpha,beta",
                    "--trials",
                    "3",
                    "--max-turns",
                    "5",
                    "--max-budget-usd",
                    "1.5",
                    "--format",
                    "json",
                ],
            )
        assert result.exit_code == 0, result.output
        (args,) = docker.call_args.args
        assert args[0] == "benchmark"
        flag_values = {args[i]: args[i + 1] for i in range(1, len(args) - 1) if args[i].startswith("--")}
        assert flag_values["--models"] == "claude-opus-4-8@xhigh,claude-fable-5@medium"
        assert flag_values["--scenarios"] == "alpha,beta"
        assert flag_values["--trials"] == "3"
        assert flag_values["--max-turns"] == "5"
        assert flag_values["--max-budget-usd"] == "1.5"
        assert flag_values["--format"] == "json"
        # the re-routed in-container invocation must NOT re-route again
        assert "--local" not in args

    def test_local_escape_runs_in_process_without_docker(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.cli.eval.benchmark.discover_specs", return_value=specs),
            patch("teatree.cli.eval.benchmark.SdkInProcessRunner", _BenchmarkRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
            patch("teatree.cli.eval.benchmark.run_eval_in_docker") as docker,
        ):
            result = CliRunner().invoke(app, ["eval", "benchmark", "--models", "opus@xhigh", "--no-persist", "--local"])
        assert result.exit_code == 0, result.output
        docker.assert_not_called()
        assert "WARNING" in result.output

    def test_in_container_runs_in_process_without_docker(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.benchmark.discover_specs", return_value=specs),
            patch("teatree.cli.eval.benchmark.SdkInProcessRunner", _BenchmarkRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
            patch("teatree.cli.eval.benchmark.run_eval_in_docker") as docker,
        ):
            # The autouse fixture's T3_EVAL_IN_CONTAINER=1 is exactly this case.
            result = CliRunner().invoke(app, ["eval", "benchmark", "--models", "opus@xhigh", "--no-persist"])
        assert result.exit_code == 0, result.output
        docker.assert_not_called()

    def test_docker_unavailable_without_local_exits_2(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.cli.eval.benchmark.run_eval_in_docker", side_effect=DockerUnavailableError),
        ):
            result = CliRunner().invoke(app, ["eval", "benchmark", "--models", "opus@xhigh"])
        assert result.exit_code == 2
        assert "docker" in result.output.lower()


class _CostRunner:
    """An sdk runner whose per-scenario cost is fixed at construction."""

    cost = 0.05

    def __init__(self, *_: object, **__: object) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name, tool_calls=_PASSING_CALL, cost_usd=type(self).cost)


@pytest.mark.django_db
class TestMatrixCostRegressionGate:
    """`--gate-cost-regression` must NOT be silently inert for `--models`/`--trials`."""

    def _cheap_baseline(self, model: str) -> None:
        class _Cheap(_CostRunner):
            cost = 0.10

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha")]),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _Cheap),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            CliRunner().invoke(app, ["eval", "run", "--models", model, "--baseline"])

    def test_models_lane_fails_on_a_cost_blowup(self) -> None:
        self._cheap_baseline("opus")

        class _Spendy(_CostRunner):
            cost = 1.00

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha")]),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _Spendy),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus", "--gate-cost-regression"])
        assert result.exit_code == 1, result.output
        assert "COST REGRESSED" in result.output

    def test_models_lane_passes_when_cost_is_flat(self) -> None:
        self._cheap_baseline("opus")
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha")]),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _CostRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus", "--gate-cost-regression"])
        # cost fell (0.05 < 0.10), no regression
        assert result.exit_code == 0, result.output
        assert "COST REGRESSED" not in result.output

    def test_trials_lane_fails_on_a_cost_blowup(self) -> None:
        class _Cheap(_CostRunner):
            cost = 0.10

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha")]),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _Cheap),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--baseline"])

        class _Spendy(_CostRunner):
            cost = 1.00

        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=[_spec("alpha")]),
            patch("teatree.cli.eval.multi_trial.SdkInProcessRunner", _Spendy),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--gate-cost-regression"])
        assert result.exit_code == 1, result.output
        assert "COST REGRESSED" in result.output


class TestPrepareSubscription:
    def test_emits_prompt_and_transcript_path(self, tmp_path: Path) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "prepare-subscription", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert str(tmp_path / "alpha.jsonl") in result.output

    def test_json_format_lists_manifest(self, tmp_path: Path) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.app.discover_specs", return_value=specs):
            result = CliRunner().invoke(
                app,
                ["eval", "prepare-subscription", "--format", "json", "--transcript-dir", str(tmp_path)],
            )
        assert result.exit_code == 0, result.output
        # An update banner ("[update] …") can precede stdout; isolate the JSON
        # array by its indented-object opener rather than the first '['.
        start = result.output.index("[\n")
        end = result.output.rindex("]") + 1
        manifest = json.loads(result.output[start:end])
        assert manifest[0]["scenario"] == "alpha"
        assert manifest[0]["transcript_path"] == str(tmp_path / "alpha.jsonl")


def _good_trigger() -> TriggerQAReport:
    return TriggerQAReport(checks=(TriggerCheck("debug", "the build is broken", should_fire=True, fired=True),))


def _bad_trigger() -> TriggerQAReport:
    return TriggerQAReport(checks=(TriggerCheck("debug", "no scope", should_fire=True, fired=False),))


def _regression(*, ok: bool) -> RegressionReport:
    check = RegressionCheck(
        failure_class="synthetic",
        origin="https://example.com/x",
        invariant="inv",
        predicate=lambda: ok,
    )
    return RegressionReport(results=(CheckResult(check=check, ok=ok, skipped=False, detail="" if ok else "violated"),))


def _negative_outcome(*, caught: bool) -> NegativeControlOutcome:
    matchers = (_matcher_result(passed=not caught),)
    return NegativeControlOutcome(
        scenario_name="worktree_first",
        result=ScenarioResult(
            spec=_spec("worktree_first"),
            run=_run("worktree_first"),
            matcher_results=matchers,
            skipped=False,
        ),
        offending_tool_call=None,
    )


def _matcher_result(*, passed: bool) -> MatcherResult:
    return MatcherResult(
        matcher=Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),
        passed=passed,
        message="ok" if passed else "violation",
    )


def _coverage(*, gaps: tuple[str, ...] = ()) -> CoverageReport:
    rows = [SkillCoverage(skill="ship", covered=True, scenario_count=2, exempt=False, exempt_reason=None)]
    rows += [SkillCoverage(skill=g, covered=False, scenario_count=0, exempt=False, exempt_reason=None) for g in gaps]
    return CoverageReport(rows=tuple(rows))


def _command_validity(*, ok: bool = True) -> CommandValidityReport:
    if ok:
        return CommandValidityReport(violations=(), checked=3)
    violation = CommandViolation(skill="stale", doc="stale/SKILL.md", command="t3 frobnicate")
    return CommandValidityReport(violations=(violation,), checked=3)


def _prose_report(*, weakest: str = "beta") -> ProseJudgeReport:
    return ProseJudgeReport(
        scores=(ProseScore(skill=weakest, score=0.2, rationale="advisory"), ProseScore("alpha", 0.9, "ok")),
        skipped=0,
    )


@contextmanager
def _patch_all_lanes(  # noqa: PLR0913 — one keyword per free lane the `eval all` run patches; the list IS the lane set.
    specs: list[EvalSpec],
    *,
    trigger: TriggerQAReport | None = None,
    regression_ok: bool = True,
    negative_caught: bool = True,
    replay_results: list[InvariantResult] | None = None,
    coverage_gaps: tuple[str, ...] = (),
    command_validity_ok: bool = True,
) -> "Iterator[None]":
    """Patch every free-lane input `run_full_suite` (in cli.eval.all) resolves."""
    with (
        patch("teatree.cli.eval.all.discover_specs", return_value=specs),
        patch("teatree.cli.eval.all.run_trigger_qa", return_value=trigger or _good_trigger()),
        patch("teatree.cli.eval.all.skill_eval_coverage", return_value=_coverage(gaps=coverage_gaps)),
        patch("teatree.cli.eval.all.run_regression_corpus", return_value=_regression(ok=regression_ok)),
        patch("teatree.cli.eval.all.run_negative_control", return_value=_negative_outcome(caught=negative_caught)),
        patch("teatree.cli.eval.all.replay_transcript_for_all", return_value=replay_results),
        patch(
            "teatree.cli.eval.all.validate_shipped_skill_commands",
            return_value=_command_validity(ok=command_validity_ok),
        ),
        patch("teatree.cli.eval.all.run_prose_judge", return_value=_prose_report()),
    ):
        yield


class TestEvalDefault:
    """Bare ``t3 eval`` (no subcommand, no args) runs the ENTIRE suite.

    The user's whole ask: ``t3 eval`` with nothing else runs everything in one
    go with a single aggregated summary; subcommands/args are the targeted path.
    """

    def test_bare_eval_runs_all_lanes_and_renders_unified_table(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert any(ch in result.output for ch in "─│┌┐└┘╭╮╰╯"), result.output
        for lane in (
            "skill-triggers",
            "skill-coverage",
            "pinned-regressions",
            "negative-control",
            "transcript-replay",
            "corpus-grade",
        ):
            assert lane in result.output, f"missing lane {lane!r}: {result.output}"

    def test_bare_eval_with_no_args_at_all_runs_the_suite(self) -> None:
        # The literal no-argument invocation: `t3 eval` and nothing else.
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval"])
        assert result.exit_code == 0, result.output
        assert "pinned-regressions" in result.output
        assert "negative-control" in result.output

    def test_bare_eval_exits_nonzero_when_a_lane_fails(self) -> None:
        with _patch_all_lanes([_spec("worktree_first")], negative_caught=False):
            result = CliRunner().invoke(app, ["eval"])
        assert result.exit_code == 1, result.output

    def test_bare_eval_exits_nonzero_on_a_trigger_failure(self) -> None:
        with _patch_all_lanes([_spec("worktree_first")], trigger=_bad_trigger()):
            result = CliRunner().invoke(app, ["eval"])
        assert result.exit_code == 1, result.output

    def test_bare_eval_help_is_still_reachable(self) -> None:
        result = CliRunner().invoke(app, ["eval", "--help"])
        assert result.exit_code == 0, result.output
        assert "run" in result.output
        assert "negative-control" in result.output

    def test_bare_eval_docker_delegates_to_the_container(self) -> None:
        with (
            patch("teatree.cli.eval.all.run_eval_in_docker", return_value=0) as run_docker,
            patch("teatree.cli.eval.all.run_trigger_qa", side_effect=AssertionError("docker must not run host lanes")),
        ):
            result = CliRunner().invoke(app, ["eval", "--docker", "--free-only"])
        assert result.exit_code == 0, result.output
        run_docker.assert_called_once()
        assert run_docker.call_args.args[0] == ["all", "--free-only"]

    def test_bare_eval_docker_propagates_container_exit_code(self) -> None:
        with patch("teatree.cli.eval.all.run_eval_in_docker", return_value=1):
            result = CliRunner().invoke(app, ["eval", "--docker"])
        assert result.exit_code == 1, result.output

    def test_bare_eval_docker_unavailable_exits_code_2(self) -> None:
        with patch("teatree.cli.eval.all.run_eval_in_docker", side_effect=DockerUnavailableError):
            result = CliRunner().invoke(app, ["eval", "--docker"])
        assert result.exit_code == 2
        assert "docker is not on PATH" in result.output


class TestEvalSubcommandsStillWork:
    """Subcommands/args remain the special, targeted path (capability kept)."""

    def test_run_subcommand_still_runs_one_scenario(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.app.find_spec", return_value=specs[0]),
            patch("teatree.eval.backends.SdkInProcessRunner", _PassRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "alpha", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output

    def test_pinned_regressions_subcommand_still_works(self) -> None:
        check = RegressionCheck(
            failure_class="synthetic",
            origin="https://example.com/x",
            invariant="ok",
            predicate=lambda: True,
        )
        good = RegressionReport(results=(CheckResult(check=check, ok=True, skipped=False, detail=""),))
        with patch("teatree.cli.eval.lanes.run_regression_corpus", return_value=good):
            result = CliRunner().invoke(app, ["eval", "pinned-regressions"])
        assert result.exit_code == 0, result.output
        assert "PASS synthetic" in result.output

    def test_negative_control_subcommand_still_works(self) -> None:
        result = CliRunner().invoke(app, ["eval", "negative-control"])
        assert result.exit_code == 0, result.output
        assert "worktree_first" in result.output

    def test_all_subcommand_still_works(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "pinned-regressions" in result.output


class TestEvalAll:
    def test_runs_free_lanes_and_renders_unified_table(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert any(ch in result.output for ch in "─│┌┐└┘╭╮╰╯"), result.output
        assert "skill-triggers" in result.output
        assert "pinned-regressions" in result.output

    def test_table_lists_all_lanes_including_coverage(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        lanes = (
            "skill-triggers",
            "skill-coverage",
            "pinned-regressions",
            "negative-control",
            "transcript-replay",
            "corpus-grade",
            "skill-command-validity",
            "ai-eval",
            "skill-prose-judge",
        )
        for lane in lanes:
            assert lane in result.output, f"missing lane {lane!r}: {result.output}"

    def test_coverage_gap_is_warn_first_exit_zero(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")], coverage_gaps=("loops",)):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "skill-coverage" in result.output

    def test_free_only_drops_the_ai_lane(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        for lane in (
            "skill-triggers",
            "skill-coverage",
            "pinned-regressions",
            "negative-control",
            "transcript-replay",
            "corpus-grade",
            "skill-command-validity",
        ):
            assert lane in result.output, f"missing free lane {lane!r}: {result.output}"
        assert "ai-eval" not in result.output, result.output
        assert "skill-prose-judge" not in result.output, result.output  # Tier-3 is metered, dropped by --free-only

    def test_free_only_never_discovers_specs_or_meters(self, tmp_path: Path) -> None:
        with (
            _patch_all_lanes([_spec("worktree_first")]),
            patch("teatree.cli.eval.all.run_ai_lane", side_effect=AssertionError("free-only must not run the AI lane")),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output

    def test_free_only_still_fails_on_a_deterministic_violation(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")], negative_caught=False):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 1, result.output

    def test_docker_delegates_to_the_container_and_skips_host_lanes(self) -> None:
        with (
            patch("teatree.cli.eval.all.run_eval_in_docker", return_value=0) as run_docker,
            patch("teatree.cli.eval.all.run_trigger_qa", side_effect=AssertionError("docker must not run host lanes")),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--docker", "--free-only"])
        assert result.exit_code == 0, result.output
        run_docker.assert_called_once()
        assert run_docker.call_args.args[0] == ["all", "--free-only"]

    def test_docker_propagates_container_exit_code(self) -> None:
        with patch("teatree.cli.eval.all.run_eval_in_docker", return_value=1):
            result = CliRunner().invoke(app, ["eval", "all", "--docker"])
        assert result.exit_code == 1, result.output

    def test_docker_passes_non_default_backend_through(self) -> None:
        with patch("teatree.cli.eval.all.run_eval_in_docker", return_value=0) as run_docker:
            result = CliRunner().invoke(app, ["eval", "all", "--docker", "--backend", "sdk"])
        assert result.exit_code == 0, result.output
        assert run_docker.call_args.args[0] == ["all", "--backend", "sdk"]

    def test_docker_unavailable_exits_code_2(self) -> None:
        with patch("teatree.cli.eval.all.run_eval_in_docker", side_effect=DockerUnavailableError):
            result = CliRunner().invoke(app, ["eval", "all", "--docker"])
        assert result.exit_code == 2
        assert "docker is not on PATH" in result.output


class TestEvalFinalVerdict:
    """Every ``t3 eval`` / ``t3 eval all`` run ends with a plain-language verdict.

    A non-expert reader must be able to tell from the LAST lines whether the run
    was all-good, found a real problem, or could not fully validate (the AI lane
    was skipped for setup reasons). The verdict is honest: a setup-skip is never
    rendered as a pass-with-no-caveat, and a real failure names the failing lane.
    """

    def test_all_pass_renders_all_good_verdict(self, tmp_path: Path) -> None:
        # Free-only so every lane truly passes (no AI lane to caveat).
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "✅ ALL GOOD" in result.output, result.output
        assert "every check passed" in result.output, result.output

    def test_real_failure_renders_problems_and_names_the_lane(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")], negative_caught=False):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "❌ PROBLEMS FOUND" in result.output, result.output
        assert "negative-control" in result.output, result.output

    def test_ai_lane_skipped_renders_needs_setup_not_failed(self, tmp_path: Path) -> None:
        # Default backend, no transcripts on disk -> the AI lane cannot run.
        with (
            _patch_all_lanes([_spec("worktree_first")]),
            patch("teatree.eval.backends.SdkInProcessRunner", side_effect=AssertionError("must not meter")),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "SKIPPED" in result.output, result.output
        assert "needs setup" in result.output, result.output
        # The AI lane is NOT rendered as a failure.
        assert "❌ PROBLEMS FOUND" not in result.output, result.output

    def test_ai_lane_skipped_verdict_flags_not_validated(self, tmp_path: Path) -> None:
        with (
            _patch_all_lanes([_spec("worktree_first")]),
            patch("teatree.eval.backends.SdkInProcessRunner", side_effect=AssertionError("must not meter")),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        # Deterministic part is good, AND the reader is told the AI lane was NOT run.
        assert "Deterministic checks" in result.output, result.output
        assert "ALL GOOD" in result.output, result.output
        assert "NOT RUN" in result.output, result.output
        assert "not yet validated" in result.output, result.output

    def test_strict_makes_a_setup_skipped_lane_exit_nonzero(self, tmp_path: Path) -> None:
        with (
            _patch_all_lanes([_spec("worktree_first")]),
            patch("teatree.eval.backends.SdkInProcessRunner", side_effect=AssertionError("must not meter")),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--strict", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 1, result.output

    def test_strict_stays_green_when_everything_actually_passes(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(
                app, ["eval", "all", "--strict", "--free-only", "--transcript-dir", str(tmp_path)]
            )
        assert result.exit_code == 0, result.output
        assert "✅ ALL GOOD" in result.output, result.output

    def test_bare_eval_default_also_renders_the_verdict(self, tmp_path: Path) -> None:
        with (
            _patch_all_lanes([_spec("worktree_first")]),
            patch("teatree.eval.backends.SdkInProcessRunner", side_effect=AssertionError("must not meter")),
        ):
            result = CliRunner().invoke(app, ["eval", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "NOT RUN" in result.output, result.output


class TestEvalRunDocker:
    """``t3 eval run --docker`` — the metered sdk lane runs in-container, not on the host."""

    def test_delegates_metered_run_to_the_container(self) -> None:
        with (
            patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_docker,
            patch("teatree.cli.eval.app.discover_specs", side_effect=AssertionError("docker must not run on the host")),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--require-executed", "--docker"])
        assert result.exit_code == 0, result.output
        run_docker.assert_called_once()
        assert run_docker.call_args.args[0] == [
            "run",
            "--max-budget-usd",
            "1.0",
            "--effort",
            "high",
            "--backend",
            "sdk",
            "--require-executed",
            "--no-persist",
        ]

    def test_forwards_scenario_name_and_trials(self) -> None:
        with patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_docker:
            CliRunner().invoke(app, ["eval", "run", "alpha", "--trials", "3", "--docker"])
        assert run_docker.call_args.args[0] == [
            "run",
            "alpha",
            "--max-budget-usd",
            "1.0",
            "--effort",
            "high",
            "--trials",
            "3",
            "--require",
            "any",
            "--no-persist",
        ]

    def test_forwards_effort_into_the_container(self) -> None:
        with patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_docker:
            CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--effort", "max", "--docker"])
        forwarded = run_docker.call_args.args[0]
        assert forwarded[forwarded.index("--effort") + 1] == "max"

    def test_rejects_an_unknown_effort_level(self) -> None:
        result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--effort", "turbo", "--docker"])
        assert result.exit_code == 2, result.output
        assert "unknown --effort 'turbo'" in result.output

    def test_forwards_max_budget_usd_into_the_container(self) -> None:
        with patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_docker:
            CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--max-budget-usd", "1.5", "--docker"])
        forwarded = run_docker.call_args.args[0]
        assert forwarded[forwarded.index("--max-budget-usd") + 1] == "1.5"

    def test_propagates_container_exit_code(self) -> None:
        with patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=1):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--docker"])
        assert result.exit_code == 1, result.output

    def test_rejects_durable_history_flags(self) -> None:
        with patch("teatree.cli.eval.run_docker.run_eval_in_docker") as run_docker:
            result = CliRunner().invoke(app, ["eval", "run", "--baseline", "--docker"])
        assert result.exit_code == 2
        run_docker.assert_not_called()
        assert "ephemeral container" in result.output

    def test_rejects_cost_regression_gate_in_docker(self) -> None:
        with patch("teatree.cli.eval.run_docker.run_eval_in_docker") as run_docker:
            result = CliRunner().invoke(app, ["eval", "run", "--gate-cost-regression", "--docker"])
        assert result.exit_code == 2
        run_docker.assert_not_called()
        assert "ephemeral container" in result.output

    def test_docker_unavailable_exits_code_2(self) -> None:
        with patch("teatree.cli.eval.run_docker.run_eval_in_docker", side_effect=DockerUnavailableError):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--docker"])
        assert result.exit_code == 2
        assert "docker is not on PATH" in result.output


class TestEvalRunMeteredDockerByDefault:
    """``t3 eval run --backend sdk`` is metered → it defaults to running IN the container.

    The autouse fixture sets ``T3_EVAL_IN_CONTAINER=1``; these tests clear it
    (``patch.dict(..., clear=True)``) to exercise the host-side routing decision.
    """

    def test_sdk_run_routes_to_docker_by_default(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_docker,
            patch("teatree.cli.eval.app.discover_specs", side_effect=AssertionError("must not run on the host")),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0, result.output
        run_docker.assert_called_once()

    def test_trials_route_to_docker_by_default(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_docker,
        ):
            result = CliRunner().invoke(app, ["eval", "run", "alpha", "--trials", "3"])
        assert result.exit_code == 0, result.output
        run_docker.assert_called_once()

    def test_subscription_run_stays_host_default(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.cli.eval.run_docker.run_eval_in_docker") as run_docker,
            patch("teatree.eval.backends.SubscriptionTranscriptRunner") as sub,
        ):
            sub.return_value.run.return_value = _run("alpha", terminal_reason="skipped: x")
            sub.return_value.transcript_path.return_value = Path("/tmp/none.jsonl")
            CliRunner().invoke(app, ["eval", "run", "--no-persist"])
        run_docker.assert_not_called()

    def test_local_escape_runs_sdk_in_process_with_warning(self) -> None:
        specs = [_spec("alpha")]

        class _Stub:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.cli.eval.app.discover_specs", return_value=specs),
            patch("teatree.eval.backends.SdkInProcessRunner", _Stub),
            patch("teatree.cli.eval.run_docker.run_eval_in_docker") as run_docker,
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist", "--local"])
        assert result.exit_code == 0, result.output
        run_docker.assert_not_called()
        assert "WARNING" in result.output

    def test_transcript_replay_skips_not_fails_when_no_transcript(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")], replay_results=None):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "transcript-replay" in result.output
        assert "SKIP" in result.output

    def test_negative_control_failure_exits_nonzero(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")], negative_caught=False):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 1, result.output

    def test_transcript_replay_violation_exits_nonzero(self, tmp_path: Path) -> None:
        violation = [InvariantResult(ok=False, offending_index=2, message="inv")]
        with _patch_all_lanes([_spec("worktree_first")], replay_results=violation):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 1, result.output

    def test_skips_alone_keep_exit_zero(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")], replay_results=None):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output

    def test_no_transcripts_emits_manifest_never_meters(self, tmp_path: Path) -> None:
        with (
            _patch_all_lanes([_spec("worktree_first")]),
            patch("teatree.eval.backends.SdkInProcessRunner", side_effect=AssertionError("must not meter")),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert str(tmp_path / "worktree_first.jsonl") in result.output
        assert "running-evals" in result.output

    def test_grades_present_subscription_transcript(self, tmp_path: Path) -> None:
        specs = [_spec("worktree_first")]
        transcript = (Path(__file__).parents[1] / "eval" / "fixtures" / "worktree_first_pass.stream.jsonl").read_text(
            encoding="utf-8"
        )
        (tmp_path / "worktree_first.jsonl").write_text(transcript, encoding="utf-8")
        with _patch_all_lanes(specs):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "ai-eval" in result.output

    def test_failing_free_lane_exits_nonzero(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")], trigger=_bad_trigger()):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 1, result.output

    def test_sdk_backend_is_explicit_metered_opt_in(self, tmp_path: Path) -> None:
        with (
            _patch_all_lanes([_spec("alpha")]),
            patch("teatree.eval.backends.SdkInProcessRunner", _PassRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--backend", "sdk", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "ai-eval" in result.output

    def test_unknown_backend_exits_code_2(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("alpha")]):
            result = CliRunner().invoke(app, ["eval", "all", "--backend", "magic", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 2
        assert "unknown eval backend" in result.output


class TestEvalPinnedRegressions:
    def test_passing_corpus_renders_pass_and_exits_zero(self) -> None:
        check = RegressionCheck(
            failure_class="synthetic",
            origin="https://example.com/x",
            invariant="ok",
            predicate=lambda: True,
        )
        good = RegressionReport(results=(CheckResult(check=check, ok=True, skipped=False, detail=""),))
        with patch("teatree.cli.eval.lanes.run_regression_corpus", return_value=good):
            result = CliRunner().invoke(app, ["eval", "pinned-regressions"])
        assert result.exit_code == 0, result.output
        assert "0 failed" in result.output
        assert "PASS synthetic" in result.output

    def test_reports_failure_and_exits_nonzero(self) -> None:
        check = RegressionCheck(
            failure_class="synthetic",
            origin="https://example.com/x",
            invariant="never",
            predicate=lambda: False,
        )
        result_row = CheckResult(check=check, ok=False, skipped=False, detail="invariant violated")
        bad = RegressionReport(results=(result_row,))
        with patch("teatree.cli.eval.lanes.run_regression_corpus", return_value=bad):
            result = CliRunner().invoke(app, ["eval", "pinned-regressions"])
        assert result.exit_code == 1
        assert "FAIL synthetic" in result.output

    def test_json_format_emits_checks(self) -> None:
        check = RegressionCheck(
            failure_class="synthetic",
            origin="https://example.com/x",
            invariant="ok",
            predicate=lambda: True,
        )
        good = RegressionReport(results=(CheckResult(check=check, ok=True, skipped=False, detail=""),))
        with patch("teatree.cli.eval.lanes.run_regression_corpus", return_value=good):
            result = CliRunner().invoke(app, ["eval", "pinned-regressions", "--format", "json"])
        assert result.exit_code == 0
        output = result.output
        payload = json.loads(output[output.index("{") : output.rindex("}") + 1])
        assert payload["ok"] is True
        assert payload["checks"][0]["failure_class"] == "synthetic"
        assert payload["checks"][0]["origin"].startswith("https://")

    def test_old_regression_command_is_gone(self) -> None:
        result = CliRunner().invoke(app, ["eval", "regression"])
        assert result.exit_code != 0, result.output


class TestEvalNegativeControl:
    def test_exits_zero_when_harness_catches_the_planted_violation(self) -> None:
        result = CliRunner().invoke(app, ["eval", "negative-control"])
        assert result.exit_code == 0
        assert "worktree_first" in result.output
        assert "Edit" in result.output

    def test_exits_nonzero_when_harness_fails_to_catch(self) -> None:
        outcome = NegativeControlOutcome(
            scenario_name="worktree_first",
            result=ScenarioResult(
                spec=_spec("worktree_first"),
                run=_run("worktree_first"),
                matcher_results=(),
                skipped=False,
            ),
            offending_tool_call=None,
        )
        with patch("teatree.cli.eval.negative_control.run_negative_control", return_value=outcome):
            result = CliRunner().invoke(app, ["eval", "negative-control"])
        assert result.exit_code == 1

    def test_json_format_reports_caught_and_offending_call(self) -> None:
        result = CliRunner().invoke(app, ["eval", "negative-control", "--format", "json"])
        assert result.exit_code == 0
        output = result.output
        payload = json.loads(output[output.index("{") : output.rindex("}") + 1])
        assert payload["caught"] is True
        assert payload["scenario"] == "worktree_first"
        assert payload["offending_tool_call"]["name"] == "Edit"

    def test_unknown_format_exits_with_code_2(self) -> None:
        result = CliRunner().invoke(app, ["eval", "negative-control", "--format", "yaml"])
        assert result.exit_code == 2
        assert "unknown --format" in result.output


class TestEvalCoverage:
    def test_clean_corpus_exits_zero_and_renders_table(self) -> None:
        report = _coverage()
        with patch("teatree.cli.eval.lanes.skill_eval_coverage", return_value=report):
            result = CliRunner().invoke(app, ["eval", "coverage"])
        assert result.exit_code == 0, result.output
        assert "ship" in result.output
        assert "0 gap(s)" in result.output

    def test_gap_is_warn_first_exit_zero_by_default(self) -> None:
        with patch("teatree.cli.eval.lanes.skill_eval_coverage", return_value=_coverage(gaps=("loops",))):
            result = CliRunner().invoke(app, ["eval", "coverage"])
        assert result.exit_code == 0, result.output
        assert "loops" in result.output
        assert "1 gap(s)" in result.output

    def test_fail_on_gap_exits_nonzero(self) -> None:
        with patch("teatree.cli.eval.lanes.skill_eval_coverage", return_value=_coverage(gaps=("loops",))):
            result = CliRunner().invoke(app, ["eval", "coverage", "--fail-on-gap"])
        assert result.exit_code == 1, result.output

    def test_fail_on_gap_with_clean_corpus_exits_zero(self) -> None:
        with patch("teatree.cli.eval.lanes.skill_eval_coverage", return_value=_coverage()):
            result = CliRunner().invoke(app, ["eval", "coverage", "--fail-on-gap"])
        assert result.exit_code == 0, result.output

    def test_json_format_lists_gaps(self) -> None:
        with patch("teatree.cli.eval.lanes.skill_eval_coverage", return_value=_coverage(gaps=("loops",))):
            result = CliRunner().invoke(app, ["eval", "coverage", "--format", "json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output[result.output.index("{") : result.output.rindex("}") + 1])
        assert payload["gaps"] == ["loops"]

    def test_unknown_format_exits_with_code_2(self) -> None:
        with patch("teatree.cli.eval.lanes.skill_eval_coverage", return_value=_coverage()):
            result = CliRunner().invoke(app, ["eval", "coverage", "--format", "yaml"])
        assert result.exit_code == 2
        assert "unknown --format" in result.output


class TestEvalAllCorpusGradeLane:
    """The deterministic corpus part runs as a free lane in the full suite."""

    def test_corpus_grade_lane_runs_in_the_free_suite(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "corpus-grade" in result.output
        assert "judge-skipped" in result.output

    def test_failing_corpus_grade_fails_the_suite(self, tmp_path: Path) -> None:
        failing = [CorpusGradeRow(entry_id="x_entry", oracle="matcher", verdict="fail", detail="1/1 matchers failed")]
        with (
            _patch_all_lanes([_spec("worktree_first")]),
            patch("teatree.cli.eval.all.grade_shipped_corpus", return_value=failing),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "corpus-grade" in result.output


class TestEvalAllSkillCommandValidityLane:
    """Tier-1 (#550) runs as a free lane and FAILs on a stale `t3 …` reference."""

    def test_command_validity_lane_runs_in_the_free_suite(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "skill-command-validity" in result.output

    def test_stale_command_reference_fails_the_suite(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")], command_validity_ok=False):
            result = CliRunner().invoke(app, ["eval", "all", "--free-only", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "skill-command-validity" in result.output


class TestEvalAllSkillProseJudgeLaneAdvisory:
    """Tier-3 (#550) runs on the metered path but is ADVISORY — never fails the suite."""

    def test_prose_judge_lane_runs_on_the_metered_path(self, tmp_path: Path) -> None:
        with _patch_all_lanes([_spec("worktree_first")]):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "skill-prose-judge" in result.output
        assert "advisory" in result.output.lower()

    def test_low_prose_score_does_not_fail_the_suite(self, tmp_path: Path) -> None:
        # a weakest skill scored 0.2 is rendered + nominated but never fails the run
        weak = ProseJudgeReport(scores=(ProseScore("worst", 0.0, "advisory"),), skipped=0)
        with (
            _patch_all_lanes([_spec("worktree_first")]),
            patch("teatree.cli.eval.all.run_prose_judge", return_value=weak),
        ):
            result = CliRunner().invoke(app, ["eval", "all", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
