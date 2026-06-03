"""``t3 eval list`` / ``t3 eval run`` end-to-end through the typer CLI."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.regression_corpus import CheckResult, RegressionCheck, RegressionReport
from teatree.eval.trigger_qa import TriggerCheck, TriggerQAReport


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
) -> EvalRun:
    return EvalRun(
        spec_name=spec_name,
        tool_calls=tool_calls,
        text_blocks=(),
        terminal_reason=terminal_reason,
        is_error=is_error,
        raw_stdout="",
        raw_stderr="",
    )


_PASSING_CALL = (EvalToolCall(name="Bash", input={"command": "git worktree add ../wt HEAD"}, turn=1),)


class TestEvalList:
    def test_lists_discovered_scenario_names(self) -> None:
        with patch("teatree.cli.eval.discover_specs", return_value=[_spec("alpha"), _spec("beta")]):
            result = CliRunner().invoke(app, ["eval", "list"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_prints_placeholder_when_no_scenarios(self) -> None:
        with patch("teatree.cli.eval.discover_specs", return_value=[]):
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS alpha" in result.output
        assert "PASS beta" in result.output

    def test_runs_one_scenario_when_name_given(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.find_spec", return_value=specs[0]),
            patch("teatree.eval.backends.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "alpha", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "PASS beta" not in result.output

    def test_unknown_scenario_exits_with_code_2_and_lists_available(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.find_spec", return_value=None),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "missing"])
        assert result.exit_code == 2
        assert "unknown scenario: 'missing'" in result.output
        assert "available scenarios: alpha, beta" in result.output

    def test_unknown_scenario_shows_none_when_empty_inventory(self) -> None:
        with (
            patch("teatree.cli.eval.discover_specs", return_value=[]),
            patch("teatree.cli.eval.find_spec", return_value=None),
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _StubRunner),
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _StubRunner),
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

    def test_exits_nonzero_when_any_scenario_failed(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                # No tool_calls → the positive matcher fails.
                return _run(spec.name)

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 1
        assert "FAIL alpha" in result.output

    def test_max_turns_override_is_passed_to_runner(self) -> None:
        specs = [_spec("alpha")]
        captured: dict[str, object] = {}

        class _StubRunner:
            def __init__(self, *, max_turns_override: int | None = None) -> None:
                captured["max_turns_override"] = max_turns_override

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--max-turns", "9", "--no-persist"])
        assert result.exit_code == 0
        assert captured["max_turns_override"] == 9


class TestEvalPassAtK:
    def test_trials_aggregates_and_reports_pass_rate(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--require", "all", "--no-persist"])
        assert result.exit_code == 1
        assert "FAIL alpha (1/2 trials" in result.output

    def test_bad_require_exits_code_2(self) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.discover_specs", return_value=specs):
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--format", "json", "--no-persist"])
        assert result.exit_code == 0, result.output
        output = result.output
        payload = json.loads(output[output.index("{") : output.rindex("}") + 1])
        assert payload["mode"] == "pass@2"
        assert payload["scenarios"][0]["passes"] == 2

    def test_all_trials_skipped_reports_skip(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, terminal_reason="skipped: claude binary not on PATH")

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--no-persist"])
        assert result.exit_code == 0
        assert "SKIP alpha" in result.output


class _SkippingRunner:
    def __init__(self, *_, **__) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name, terminal_reason="skipped: claude binary not on PATH")


class TestEvalRequireExecuted:
    """`--require-executed` turns a decorative (collected>0, ran==0) run red."""

    def test_single_trial_all_skipped_exits_nonzero_when_required(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--require-executed", "--no-persist"])
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_single_trial_all_skipped_stays_green_without_flag(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--no-persist"])
        assert result.exit_code == 0, result.output

    def test_single_trial_with_execution_passes_under_flag(self) -> None:
        specs = [_spec("alpha")]

        class _PassingRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _PassingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--require-executed", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS alpha" in result.output

    def test_pass_at_k_all_skipped_exits_nonzero_when_required(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "3", "--require-executed", "--no-persist"])
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_pass_at_k_all_skipped_stays_green_without_flag(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "3", "--no-persist"])
        assert result.exit_code == 0, result.output

    def test_pass_at_k_with_execution_passes_under_flag(self) -> None:
        specs = [_spec("alpha")]

        class _PassingRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _PassingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "3", "--require-executed", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS alpha" in result.output

    def test_zero_collected_stays_green_under_flag(self) -> None:
        with (
            patch("teatree.cli.eval.discover_specs", return_value=[]),
            patch("teatree.eval.backends.ClaudePRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--require-executed", "--no-persist"])
        assert result.exit_code == 0, result.output


class TestEvalTriggerQA:
    def test_shipped_corpus_passes(self) -> None:
        result = CliRunner().invoke(app, ["eval", "trigger-qa"])
        assert result.exit_code == 0, result.output
        assert "0 failed" in result.output

    def test_reports_failure_and_exits_nonzero(self) -> None:
        bad = TriggerQAReport(checks=(TriggerCheck("debug", "no scope here", should_fire=True, fired=False),))
        with patch("teatree.cli.eval.run_trigger_qa", return_value=bad):
            result = CliRunner().invoke(app, ["eval", "trigger-qa"])
        assert result.exit_code == 1
        assert "under-trigger" in result.output

    def test_json_format_emits_checks(self) -> None:
        good = TriggerQAReport(checks=(TriggerCheck("debug", "the build is broken", should_fire=True, fired=True),))
        with patch("teatree.cli.eval.run_trigger_qa", return_value=good):
            result = CliRunner().invoke(app, ["eval", "trigger-qa", "--format", "json"])
        assert result.exit_code == 0
        output = result.output
        payload = json.loads(output[output.index("{") : output.rindex("}") + 1])
        assert payload["ok"] is True
        assert payload["checks"][0]["skill"] == "debug"

    def test_over_trigger_message_for_unexpected_fire(self) -> None:
        bad = TriggerQAReport(checks=(TriggerCheck("debug", "open a PR", should_fire=False, fired=True),))
        with patch("teatree.cli.eval.run_trigger_qa", return_value=bad):
            result = CliRunner().invoke(app, ["eval", "trigger-qa"])
        assert result.exit_code == 1
        assert "over-trigger" in result.output


class _PassRunner:
    def __init__(self, *_: object, **__: object) -> None: ...

    def run(self, spec: EvalSpec) -> EvalRun:
        return _run(spec.name, tool_calls=_PASSING_CALL)


class TestEvalBackend:
    def test_unknown_backend_exits_with_code_2(self) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--backend", "magic", "--no-persist"])
        assert result.exit_code == 2
        assert "unknown eval backend" in result.output

    def test_subscription_backend_grades_a_saved_transcript(self, tmp_path: Path) -> None:
        specs = [_spec("worktree_first")]
        transcript = (Path(__file__).parents[1] / "eval" / "fixtures" / "worktree_first_pass.stream.jsonl").read_text(
            encoding="utf-8"
        )
        (tmp_path / "worktree_first.jsonl").write_text(transcript, encoding="utf-8")

        with patch("teatree.cli.eval.discover_specs", return_value=specs):
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

        with patch("teatree.cli.eval.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--transcript-dir", str(tmp_path), "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "PASS worktree_first" in result.output

    def test_default_backend_missing_transcript_prints_clear_hint(self, tmp_path: Path) -> None:
        # The missing-transcript UX: a bare run with no transcripts skips cleanly
        # (exit 0) and names the scenario, the expected path, and the recipe to
        # produce it — never a silent no-op.
        specs = [_spec("worktree_first")]
        with patch("teatree.cli.eval.discover_specs", return_value=specs):
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _PassRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--trials", "2", "--no-persist"])
        assert result.exit_code == 0, result.output
        assert "metered sdk runner" in result.output


@pytest.mark.django_db
class TestEvalPersistAndHistory:
    def test_persists_and_history_lists_it(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _PassRunner),
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _PassRunner),
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--baseline"])
            result = CliRunner().invoke(app, ["eval", "history", "--baseline"])
        assert result.exit_code == 0, result.output
        assert "[baseline]" in result.output

    def test_gate_regressions_flags_drop_against_baseline(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            first = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--baseline"])
            assert first.exit_code == 0, first.output

        class _FailRunner:
            def __init__(self, *_: object, **__: object) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name)

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.eval.backends.ClaudePRunner", _FailRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            second = CliRunner().invoke(app, ["eval", "run", "--backend", "sdk", "--gate-regressions"])

        assert second.exit_code == 1, second.output
        assert "REGRESSED alpha" in second.output


@pytest.mark.django_db
class TestEvalModelMatrix:
    def test_matrix_runs_each_model_and_renders_columns(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _PassRunner),
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _PassRunner),
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
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _FailOnHaiku),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus,haiku", "--no-persist"])
        assert result.exit_code == 1, result.output
        assert "opus: 1 passed" in result.output
        assert "haiku: 0 passed, 1 failed" in result.output

    def test_empty_models_exits_code_2(self) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "run", "--models", " , ", "--no-persist"])
        assert result.exit_code == 2
        assert "--models was empty" in result.output

    def test_matrix_persists_each_model(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _PassRunner),
            patch("teatree.eval.persistence.current_git_sha", return_value=""),
        ):
            CliRunner().invoke(app, ["eval", "run", "--models", "opus,haiku"])
            history = CliRunner().invoke(app, ["eval", "history", "--format", "json"])
        payload = json.loads(history.output[history.output.index("{") : history.output.rindex("}") + 1])
        assert payload["runs"][0]["model"] == "opus,haiku"

    def test_matrix_all_skipped_exits_nonzero_when_required(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(
                app, ["eval", "run", "--models", "opus,haiku", "--require-executed", "--no-persist"]
            )
        assert result.exit_code != 0, result.output
        assert "executed 0" in result.output

    def test_matrix_all_skipped_stays_green_without_flag(self) -> None:
        specs = [_spec("alpha")]
        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _SkippingRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--models", "opus,haiku", "--no-persist"])
        assert result.exit_code == 0, result.output


class TestPrepareSubscription:
    def test_emits_prompt_and_transcript_path(self, tmp_path: Path) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.discover_specs", return_value=specs):
            result = CliRunner().invoke(app, ["eval", "prepare-subscription", "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert str(tmp_path / "alpha.jsonl") in result.output

    def test_json_format_lists_manifest(self, tmp_path: Path) -> None:
        specs = [_spec("alpha")]
        with patch("teatree.cli.eval.discover_specs", return_value=specs):
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


class TestEvalRegression:
    def test_passing_corpus_renders_pass_and_exits_zero(self) -> None:
        check = RegressionCheck(
            failure_class="synthetic",
            origin="https://example.com/x",
            invariant="ok",
            predicate=lambda: True,
        )
        good = RegressionReport(results=(CheckResult(check=check, ok=True, skipped=False, detail=""),))
        with patch("teatree.cli.eval.run_regression_corpus", return_value=good):
            result = CliRunner().invoke(app, ["eval", "regression"])
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
        with patch("teatree.cli.eval.run_regression_corpus", return_value=bad):
            result = CliRunner().invoke(app, ["eval", "regression"])
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
        with patch("teatree.cli.eval.run_regression_corpus", return_value=good):
            result = CliRunner().invoke(app, ["eval", "regression", "--format", "json"])
        assert result.exit_code == 0
        output = result.output
        payload = json.loads(output[output.index("{") : output.rindex("}") + 1])
        assert payload["ok"] is True
        assert payload["checks"][0]["failure_class"] == "synthetic"
        assert payload["checks"][0]["origin"].startswith("https://")
