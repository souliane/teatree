import dataclasses
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.eval.models import EvalSpec, Matcher
from teatree.eval.runner import WATCHDOG_SECONDS, ClaudeCliMissingError, ClaudePRunner

FIXTURES = Path(__file__).parent / "fixtures"


def _spec(tmp_path: Path, *, max_turns: int = 3, model: str = "haiku", tools: tuple[str, ...] = ("Bash",)) -> EvalSpec:
    agent = tmp_path / "agent.md"
    agent.write_text("# fake skill\n\nbody\n", encoding="utf-8")
    return EvalSpec(
        name="worktree_first",
        scenario="agent must create a worktree first",
        agent_path=str(agent),
        prompt="Fix README.md typo.",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=tmp_path / "spec.yaml",
        model=model,
        max_turns=max_turns,
        tools=tools,
    )


@dataclasses.dataclass
class _FakeCompleted:
    stdout: str
    stderr: str = ""
    returncode: int = 0


class TestClaudePRunner:
    def test_returns_skip_run_when_claude_missing(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        with patch("teatree.eval.runner.shutil.which", return_value=None):
            result = ClaudePRunner().run(spec)
        assert result.terminal_reason.startswith("skipped:")
        assert result.is_error is False
        assert result.tool_calls == ()

    def test_require_executed_hard_errors_when_claude_missing(self, tmp_path: Path) -> None:
        # sdk + require-executed must NEVER decoratively skip: a missing claude
        # binary raises (propagates to a non-zero CLI exit), not a skip-shaped run.
        spec = _spec(tmp_path)
        with (
            patch("teatree.eval.runner.shutil.which", return_value=None),
            pytest.raises(ClaudeCliMissingError),
        ):
            ClaudePRunner(require_executed=True).run(spec)

    def test_require_executed_still_runs_when_claude_present(self, tmp_path: Path) -> None:
        # The hard-error path only fires on a MISSING binary; a present binary
        # runs normally even under require_executed.
        spec = _spec(tmp_path)
        stream = (FIXTURES / "worktree_first_pass.stream.jsonl").read_text(encoding="utf-8")
        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.utils.run.subprocess.run", return_value=_FakeCompleted(stdout=stream)),
        ):
            result = ClaudePRunner(workspace=tmp_path, require_executed=True).run(spec)
        assert result.terminal_reason == "success"
        assert result.is_error is False

    def test_builds_command_with_canonical_flags(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            return _FakeCompleted(stdout=(FIXTURES / "worktree_first_pass.stream.jsonl").read_text(encoding="utf-8"))

        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.utils.run.subprocess.run", side_effect=_fake_run),
        ):
            ClaudePRunner(workspace=tmp_path).run(spec)

        cmd = captured["cmd"]
        assert isinstance(cmd, list)
        assert cmd[0] == "/usr/local/bin/claude"
        assert cmd[1] == "-p"
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd
        assert "--max-turns" in cmd
        assert cmd[cmd.index("--max-turns") + 1] == "3"
        assert cmd[cmd.index("--max-budget-usd") + 1] == "0.10"
        assert cmd[cmd.index("--model") + 1] == "haiku"
        assert cmd[cmd.index("--fallback-model") + 1] == "claude-sonnet-4-6"
        assert "--no-session-persistence" in cmd
        assert "--disable-slash-commands" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--strict-mcp-config" in cmd
        assert cmd[cmd.index("--tools") + 1] == "Bash"
        assert cmd[cmd.index("--settings") + 1] == '{"hooks":{}}'
        assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
        assert cmd[cmd.index("--system-prompt") + 1].startswith("# fake skill")
        assert cmd[-1] == "Fix README.md typo."
        assert captured["kwargs"].get("timeout") == WATCHDOG_SECONDS

    def test_parses_stream_json_into_eval_run(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        stream = (FIXTURES / "worktree_first_pass.stream.jsonl").read_text(encoding="utf-8")
        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.utils.run.subprocess.run", return_value=_FakeCompleted(stdout=stream)),
        ):
            result = ClaudePRunner(workspace=tmp_path).run(spec)
        assert result.terminal_reason == "success"
        assert result.is_error is False
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].input["command"].startswith("git worktree add")
        assert result.tool_calls[1].turn == 2

    def test_returns_timeout_result_when_subprocess_times_out(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)

        def _raise_timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=WATCHDOG_SECONDS, output="partial", stderr="")

        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.utils.run.subprocess.run", side_effect=_raise_timeout),
        ):
            result = ClaudePRunner(workspace=tmp_path).run(spec)
        assert result.terminal_reason == "timeout"
        assert result.is_error is True

    def test_max_turns_override_takes_precedence_over_spec(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, max_turns=3)
        captured_cmd: list[str] = []

        def _capture(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return _FakeCompleted(stdout='{"type":"result","subtype":"success"}\n')

        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.utils.run.subprocess.run", side_effect=_capture),
        ):
            ClaudePRunner(workspace=tmp_path, max_turns_override=9).run(spec)
        assert captured_cmd[captured_cmd.index("--max-turns") + 1] == "9"

    def test_raises_when_agent_definition_missing(self, tmp_path: Path) -> None:
        spec = EvalSpec(
            name="bad",
            scenario="bad",
            agent_path=str(tmp_path / "does-not-exist.md"),
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            pytest.raises(FileNotFoundError),
        ):
            ClaudePRunner(workspace=tmp_path).run(spec)

    def test_raises_when_agent_definition_is_empty(self, tmp_path: Path) -> None:
        agent = tmp_path / "empty.md"
        agent.write_text("", encoding="utf-8")
        spec = EvalSpec(
            name="empty",
            scenario="empty",
            agent_path=str(agent),
            prompt="x",
            matchers=(),
            source_path=tmp_path / "spec.yaml",
        )
        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            pytest.raises(ValueError, match="empty"),
        ):
            ClaudePRunner(workspace=tmp_path).run(spec)

    def test_nonzero_returncode_with_aborted_terminal_marks_is_error(self, tmp_path: Path) -> None:
        # No `result` event in the stream → terminal_reason is "aborted".
        # Combined with returncode != 0, the runner must set is_error=True
        # (covers the post-parse re-flag at line 73-74).
        spec = _spec(tmp_path)
        stream = '{"type":"system","subtype":"init"}\n'
        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.utils.run.subprocess.run", return_value=_FakeCompleted(stdout=stream, returncode=2)),
        ):
            result = ClaudePRunner(workspace=tmp_path).run(spec)
        assert result.terminal_reason == "aborted"
        assert result.is_error is True

    def test_timeout_with_bytes_streams_decodes_to_strings(self, tmp_path: Path) -> None:
        # subprocess.TimeoutExpired may carry bytes for stdout/stderr when
        # text=False; _coerce_stream must decode them rather than blow up.
        spec = _spec(tmp_path)

        def _raise(cmd, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd=cmd,
                timeout=120,
                output=b"partial-bytes\n",
                stderr=b"err-bytes\n",
            )

        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.utils.run.subprocess.run", side_effect=_raise),
        ):
            result = ClaudePRunner(workspace=tmp_path).run(spec)
        assert result.terminal_reason == "timeout"
        assert "partial-bytes" in result.raw_stdout
        assert "err-bytes" in result.raw_stderr
