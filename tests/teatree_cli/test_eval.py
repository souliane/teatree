"""``t3 eval list`` / ``t3 eval run`` end-to-end through the typer CLI."""

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher


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
            result = CliRunner().invoke(app, ["eval", "run", "--no-persist"])
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
            result = CliRunner().invoke(app, ["eval", "run", "alpha", "--no-persist"])
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
            result = CliRunner().invoke(app, ["eval", "run", "--format", "json", "--no-persist"])
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
            result = CliRunner().invoke(app, ["eval", "run", "--no-persist"])
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
            result = CliRunner().invoke(app, ["eval", "run", "--max-turns", "9", "--no-persist"])
        assert result.exit_code == 0
        assert captured["max_turns_override"] == 9

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
