"""``t3 eval capture-subagent`` — copy a dispatched sub-agent JSONL to a scenario path."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.eval.models import EvalSpec, Matcher


def _spec(name: str = "worktree_first") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="s",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("/tmp/spec.yaml"),
    )


class TestCaptureSubagent:
    def test_copies_freshest_subagent_jsonl_to_scenario_path(self, tmp_path: Path) -> None:
        spec = _spec()
        captured = tmp_path / "source" / "agent-x.jsonl"
        captured.parent.mkdir(parents=True)
        captured.write_text("{}\n", encoding="utf-8")

        def _capture_to(target: Path, *, since: float, provenance: object) -> Path:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}\n", encoding="utf-8")
            return captured

        with (
            patch("teatree.cli.eval.capture_subagent.find_spec", return_value=spec),
            patch("teatree.cli.eval.capture_subagent.capture_to", side_effect=_capture_to) as mock_capture,
        ):
            result = CliRunner().invoke(
                app,
                ["eval", "capture-subagent", spec.name, "--transcript-dir", str(tmp_path), "--since", "1717600000"],
            )

        assert result.exit_code == 0, result.output
        assert "captured" in result.output
        target_arg = mock_capture.call_args.args[0]
        assert target_arg == tmp_path / f"{spec.name}.jsonl"

    def test_passes_since_and_provenance_through_to_capture(self, tmp_path: Path) -> None:
        spec = _spec()
        with (
            patch("teatree.cli.eval.capture_subagent.find_spec", return_value=spec),
            patch("teatree.cli.eval.capture_subagent.current_git_sha", return_value="deadbeef"),
            patch("teatree.cli.eval.capture_subagent.capture_to", return_value=Path("/x/agent.jsonl")) as mock_capture,
        ):
            result = CliRunner().invoke(
                app,
                ["eval", "capture-subagent", spec.name, "--transcript-dir", str(tmp_path), "--since", "1717600000"],
            )

        assert result.exit_code == 0, result.output
        kwargs = mock_capture.call_args.kwargs
        assert kwargs["since"] == pytest.approx(1717600000.0)
        provenance = kwargs["provenance"]
        assert provenance.scenario == spec.name
        assert provenance.prompt == spec.prompt
        assert provenance.head_sha == "deadbeef"

    def test_since_is_required(self, tmp_path: Path) -> None:
        # --since is MANDATORY (#3313): on a 24/7-loop host it is the guard against
        # grabbing a concurrent unrelated sub-agent's transcript.
        spec = _spec()
        with patch("teatree.cli.eval.capture_subagent.find_spec", return_value=spec):
            result = CliRunner().invoke(app, ["eval", "capture-subagent", spec.name, "--transcript-dir", str(tmp_path)])
        assert result.exit_code == 2
        assert "since" in result.output.lower()

    def test_exits_nonzero_when_no_subagent_transcript_found(self, tmp_path: Path) -> None:
        spec = _spec()
        with (
            patch("teatree.cli.eval.capture_subagent.find_spec", return_value=spec),
            patch("teatree.cli.eval.capture_subagent.capture_to", return_value=None),
        ):
            result = CliRunner().invoke(
                app,
                ["eval", "capture-subagent", spec.name, "--transcript-dir", str(tmp_path), "--since", "1717600000"],
            )

        assert result.exit_code == 1
        assert "no sub-agent transcript found" in result.output

    def test_unknown_scenario_exits_with_code_2(self) -> None:
        with (
            patch("teatree.cli.eval.capture_subagent.find_spec", return_value=None),
            patch("teatree.cli.eval.capture_subagent.discover_specs", return_value=[_spec("alpha")]),
        ):
            result = CliRunner().invoke(app, ["eval", "capture-subagent", "missing", "--since", "1717600000"])

        assert result.exit_code == 2
        assert "unknown scenario" in result.output
