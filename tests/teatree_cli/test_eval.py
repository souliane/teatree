"""``t3 eval list`` / ``t3 eval run`` end-to-end through the typer CLI."""

import json
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.docker import DockerUnavailableError
from teatree.eval.coverage import CoverageReport, SkillCoverage
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.negative_control import NegativeControlOutcome
from teatree.eval.persistence import persist_run
from teatree.eval.regression_corpus import CheckResult, RegressionCheck, RegressionReport
from teatree.eval.report import MatcherResult, ScenarioResult, evaluate
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
    """
    with patch("teatree.eval.backends.ensure_oauth_token", return_value="t"):
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
            def __init__(self, *, max_turns_override: int | None = None, require_executed: bool = False) -> None:
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
            def __init__(self, *, max_turns_override: int | None = None, require_executed: bool = False) -> None:
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
        assert "metered sdk runner" in result.output


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


@contextmanager
def _patch_all_lanes(  # noqa: PLR0913 — one keyword per free lane the `eval all` run patches; the list IS the lane set.
    specs: list[EvalSpec],
    *,
    trigger: TriggerQAReport | None = None,
    regression_ok: bool = True,
    negative_caught: bool = True,
    replay_results: list[InvariantResult] | None = None,
    coverage_gaps: tuple[str, ...] = (),
) -> "Iterator[None]":
    """Patch every free-lane input `run_full_suite` (in cli.eval.all) resolves."""
    with (
        patch("teatree.cli.eval.all.discover_specs", return_value=specs),
        patch("teatree.cli.eval.all.run_trigger_qa", return_value=trigger or _good_trigger()),
        patch("teatree.cli.eval.all.skill_eval_coverage", return_value=_coverage(gaps=coverage_gaps)),
        patch("teatree.cli.eval.all.run_regression_corpus", return_value=_regression(ok=regression_ok)),
        patch("teatree.cli.eval.all.run_negative_control", return_value=_negative_outcome(caught=negative_caught)),
        patch("teatree.cli.eval.all.replay_transcript_for_all", return_value=replay_results),
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
        for lane in ("skill-triggers", "skill-coverage", "pinned-regressions", "negative-control", "transcript-replay"):
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
            "ai-eval",
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
        for lane in ("skill-triggers", "skill-coverage", "pinned-regressions", "negative-control", "transcript-replay"):
            assert lane in result.output, f"missing free lane {lane!r}: {result.output}"
        assert "ai-eval" not in result.output, result.output

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
        assert run_docker.call_args.args[0] == ["run", "--backend", "sdk", "--require-executed", "--no-persist"]

    def test_forwards_scenario_name_and_trials(self) -> None:
        with patch("teatree.cli.eval.run_docker.run_eval_in_docker", return_value=0) as run_docker:
            CliRunner().invoke(app, ["eval", "run", "alpha", "--trials", "3", "--docker"])
        assert run_docker.call_args.args[0] == ["run", "alpha", "--trials", "3", "--require", "any", "--no-persist"]

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
