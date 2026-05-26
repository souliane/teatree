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
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run"])
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
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "alpha"])
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

    def test_unknown_format_exits_with_code_2(self) -> None:
        specs = [_spec("alpha")]

        class _StubRunner:
            def __init__(self, *_, **__) -> None: ...

            def run(self, spec: EvalSpec) -> EvalRun:
                return _run(spec.name, tool_calls=_PASSING_CALL)

        with (
            patch("teatree.cli.eval.discover_specs", return_value=specs),
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
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
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--format", "json"])
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
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run"])
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
            patch("teatree.cli.eval.ClaudePRunner", _StubRunner),
        ):
            result = CliRunner().invoke(app, ["eval", "run", "--max-turns", "9"])
        assert result.exit_code == 0
        assert captured["max_turns_override"] == 9
