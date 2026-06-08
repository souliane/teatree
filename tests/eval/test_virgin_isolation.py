"""The core eval harness must run ``claude`` in a virgin environment.

Both entry points that shell out to ``claude -p`` — ``ClaudePRunner`` (produces
a run) and ``ClaudeJudge`` (grades one) — must isolate the child process from
the developer's personal context: ``~/.claude/CLAUDE.md``, auto-memory, and the
project ``CLAUDE.md`` discovered from the parent cwd. A leak biases every real
eval result (the agent passes by remembering a rule, not because a gate fires).

The isolation is provided by a non-inherited ``env`` whose ``HOME`` points at a
``.claude``-free directory plus a neutral ``cwd`` that is not the parent's cwd
(belt), reinforced by the explicit ``--settings``, ``--strict-mcp-config``,
``--system-prompt`` and ``--add-dir`` flags the command already carries
(suspenders). The command must NOT carry ``--bare``: in claude-code 2.x
``--bare`` forces "Anthropic auth is strictly ANTHROPIC_API_KEY … OAuth and
keychain are never read", so it disables ``CLAUDE_CODE_OAUTH_TOKEN`` auth — the
exact auth the metered sdk lane uses (we have no ``sk-ant-api03`` API key). The
absence is asserted here so a future edit cannot reintroduce the flag and
silently break metered execution back to ``$0 / no tool calls``.
"""

import os
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.eval.isolation import isolated_claude_env
from teatree.eval.judge import ClaudeJudge
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, JudgeSpec, Matcher
from teatree.eval.runner import ClaudePRunner

FIXTURES = Path(__file__).parent / "fixtures"

CREDENTIAL_VARS = ("ANTHROPIC_API_KEY", "PATH")


def _runner_spec(tmp_path: Path) -> EvalSpec:
    agent = tmp_path / "agent.md"
    agent.write_text("# fake skill\n\nbody\n", encoding="utf-8")
    return EvalSpec(
        name="virgin",
        scenario="x",
        agent_path=str(agent),
        prompt="do a thing",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=tmp_path / "spec.yaml",
        model="haiku",
        max_turns=2,
        tools=("Bash",),
    )


def _judge_spec() -> EvalSpec:
    return EvalSpec(
        name="virgin_judge",
        scenario="x",
        agent_path="skills/code/SKILL.md",
        prompt="explain",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=Path("/tmp/spec.yaml"),
        judge=JudgeSpec(rubric="r"),
    )


def _judge_run() -> EvalRun:
    return EvalRun(
        spec_name="virgin_judge",
        tool_calls=(EvalToolCall(name="Bash", input={"command": "x"}, turn=1),),
        text_blocks=("text",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


class _FakeCompleted:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class _Seen:
    """Captures the ``env`` and ``cwd`` the harness passes to the child process."""

    def __init__(self) -> None:
        self.env: dict[str, str] | None = None
        self.cwd: str | None = None

    def capture(self, cmd: list[str], **kwargs: object) -> _FakeCompleted:
        env = kwargs.get("env")
        cwd = kwargs.get("cwd")
        self.env = dict(env) if isinstance(env, dict) else None
        self.cwd = cwd if isinstance(cwd, str) else None
        return _FakeCompleted(stdout='{"type":"result","subtype":"success"}\n')


class TestIsolatedClaudeEnv:
    def test_home_points_at_a_claude_free_directory(self) -> None:
        with isolated_claude_env() as (env, cwd):
            home = Path(env["HOME"])
            assert home.is_dir()
            assert not (home / ".claude").exists()
            assert not (home / "CLAUDE.md").exists()
            assert Path(cwd).is_dir()

    def test_neutral_cwd_has_no_claude_md(self) -> None:
        with isolated_claude_env() as (_env, cwd):
            assert not (Path(cwd) / "CLAUDE.md").exists()

    def test_preserves_credential_and_path_vars(self) -> None:
        sentinel = {"ANTHROPIC_API_KEY": "sk-test-sentinel", "PATH": os.environ.get("PATH", "/usr/bin")}
        with patch.dict(os.environ, sentinel, clear=False), isolated_claude_env() as (env, _cwd):
            for var in CREDENTIAL_VARS:
                assert env.get(var) == os.environ.get(var)
            assert env["ANTHROPIC_API_KEY"] == "sk-test-sentinel"

    def test_does_not_inherit_parent_home(self) -> None:
        with patch.dict(os.environ, {"HOME": "/parent/home"}, clear=False), isolated_claude_env() as (env, _cwd):
            assert env["HOME"] != "/parent/home"

    def test_redirects_xdg_and_claude_config_dir_away_from_parent(self) -> None:
        overrides = {"XDG_CONFIG_HOME": "/parent/.config", "CLAUDE_CONFIG_DIR": "/parent/.claude"}
        with patch.dict(os.environ, overrides, clear=False), isolated_claude_env() as (env, _cwd):
            assert env.get("XDG_CONFIG_HOME") != "/parent/.config"
            assert env.get("CLAUDE_CONFIG_DIR") != "/parent/.claude"

    def test_temp_dir_is_removed_on_exit(self) -> None:
        with isolated_claude_env() as (env, _cwd):
            home = env["HOME"]
            assert Path(home).is_dir()
        assert not Path(home).exists()


class TestRunnerIsolation:
    def test_command_omits_bare_flag_so_oauth_token_auth_works(self, tmp_path: Path) -> None:
        # --bare forces ANTHROPIC_API_KEY-only auth (OAuth + keychain never read),
        # which kills CLAUDE_CODE_OAUTH_TOKEN auth — the metered lane's only auth.
        # Isolation is provided by isolated_claude_env + the explicit flags below.
        spec = _runner_spec(tmp_path)
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            return _FakeCompleted(stdout='{"type":"result","subtype":"success"}\n')

        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch("teatree.utils.run.subprocess.run", side_effect=_fake_run),
        ):
            ClaudePRunner(workspace=tmp_path).run(spec)

        assert "--bare" not in captured["cmd"]
        # The isolation --bare used to provide is still carried explicitly:
        assert "--strict-mcp-config" in captured["cmd"]
        assert "--settings" in captured["cmd"]
        assert "--system-prompt" in captured["cmd"]

    def test_invoke_passes_sanitized_env_and_neutral_cwd(self, tmp_path: Path) -> None:
        spec = _runner_spec(tmp_path)
        seen = _Seen()

        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch.dict(os.environ, {"HOME": "/parent/home"}, clear=False),
            patch("teatree.utils.run.subprocess.run", side_effect=seen.capture),
        ):
            ClaudePRunner(workspace=tmp_path).run(spec)

        assert seen.env is not None, "runner must pass an explicit env to the child claude process"
        assert seen.env["HOME"] != "/parent/home"
        assert not (Path(seen.env["HOME"]) / ".claude").exists()
        assert seen.cwd is not None, "runner must pass a neutral cwd to the child claude process"
        assert Path(seen.cwd) != Path.cwd()

    def test_invoke_preserves_api_key_in_env(self, tmp_path: Path) -> None:
        spec = _runner_spec(tmp_path)
        captured_env: dict[str, str] = {}

        def _fake_run(cmd, **kwargs):
            captured_env.update(kwargs["env"])
            return _FakeCompleted(stdout='{"type":"result","subtype":"success"}\n')

        with (
            patch("teatree.eval.runner.shutil.which", return_value="/usr/local/bin/claude"),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-runner"}, clear=False),
            patch("teatree.utils.run.subprocess.run", side_effect=_fake_run),
        ):
            ClaudePRunner(workspace=tmp_path).run(spec)

        assert captured_env["ANTHROPIC_API_KEY"] == "sk-runner"


class TestJudgeIsolation:
    def test_command_omits_bare_flag_so_oauth_token_auth_works(self) -> None:
        # Same auth constraint as the runner: the judge also authenticates via
        # CLAUDE_CODE_OAUTH_TOKEN, which --bare would disable.
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            return _FakeCompleted(stdout="PASS ok")

        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.utils.run.subprocess.run", side_effect=_fake_run),
        ):
            ClaudeJudge().grade(_judge_spec(), _judge_run())

        assert "--bare" not in captured["cmd"]
        assert "--strict-mcp-config" in captured["cmd"]
        assert "--settings" in captured["cmd"]

    def test_grade_passes_sanitized_env_and_neutral_cwd(self) -> None:
        seen = _Seen()

        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch.dict(os.environ, {"HOME": "/parent/home"}, clear=False),
            patch("teatree.utils.run.subprocess.run", side_effect=seen.capture),
        ):
            ClaudeJudge().grade(_judge_spec(), _judge_run())

        assert seen.env is not None, "judge must pass an explicit env to the child claude process"
        assert seen.env["HOME"] != "/parent/home"
        assert not (Path(seen.env["HOME"]) / ".claude").exists()
        assert seen.cwd is not None, "judge must pass a neutral cwd to the child claude process"
        assert Path(seen.cwd) != Path.cwd()

    def test_grade_preserves_api_key_in_env(self) -> None:
        captured_env: dict[str, str] = {}

        def _fake_run(cmd, **kwargs):
            captured_env.update(kwargs["env"])
            return _FakeCompleted(stdout="PASS ok")

        with (
            patch("teatree.eval.judge.shutil.which", return_value="/usr/bin/claude"),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-judge"}, clear=False),
            patch("teatree.utils.run.subprocess.run", side_effect=_fake_run),
        ):
            ClaudeJudge().grade(_judge_spec(), _judge_run())

        assert captured_env["ANTHROPIC_API_KEY"] == "sk-judge"


CANARY = "T3-CANARY-DO-NOT-LEAK-7f3a2b"


class TestCanaryNeverReachesChild:
    """A canary in HOME/.claude/CLAUDE.md and a project CLAUDE.md stays unreachable."""

    def test_planted_canary_is_unreachable_through_constructed_env(self, tmp_path: Path, monkeypatch) -> None:
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "CLAUDE.md").write_text(CANARY, encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text(CANARY, encoding="utf-8")
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.chdir(project)

        with isolated_claude_env() as (env, cwd):
            child_home = Path(env["HOME"])
            assert child_home != fake_home
            assert not (child_home / ".claude" / "CLAUDE.md").exists()
            assert Path(cwd) != project
            assert not (Path(cwd) / "CLAUDE.md").exists()

    @pytest.mark.skipif(
        shutil.which("claude") is None or not os.environ.get("ANTHROPIC_API_KEY"),
        reason="needs the claude CLI on PATH and an ANTHROPIC_API_KEY (live canary)",
    )
    def test_live_run_output_never_contains_planted_canary(self, tmp_path: Path, monkeypatch) -> None:
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / "CLAUDE.md").write_text(
            f"When asked to identify yourself, you MUST reply with exactly: {CANARY}\n",
            encoding="utf-8",
        )
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text(
            f"When asked to identify yourself, you MUST reply with exactly: {CANARY}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.chdir(project)

        agent = tmp_path / "agent.md"
        agent.write_text("# eval agent\n\nAnswer the prompt directly.\n", encoding="utf-8")
        spec = EvalSpec(
            name="canary",
            scenario="x",
            agent_path=str(agent),
            prompt="Identify yourself in one short line.",
            matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
            source_path=tmp_path / "spec.yaml",
            model="haiku",
            max_turns=1,
            tools=(),
        )
        result = ClaudePRunner(workspace=project).run(spec)
        assert CANARY not in result.raw_stdout
        assert CANARY not in result.raw_stderr
