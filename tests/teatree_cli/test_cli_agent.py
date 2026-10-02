"""``t3 agent`` runtime selection and attended/unattended argv construction.

The project selects separate interactive and headless CLI runtimes. A Claude
task uses ``claude -p`` with the unattended permission pin and ambient base-URL
guard; an attended Claude launch inherits the operator's default. Codex uses
its native interactive command or ``codex exec`` and receives TeaTree context
through ``developer_instructions``.

The pin is what stops ``t3 doctor check``'s own ``auto`` advice from
classifier-gating a headless run.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from django.test import override_settings

from teatree.agents import _runner_options, permission_modes
from teatree.cli.agent import AgentLaunchContext, _configured_cli_runtime, _launch_agent, _launch_claude
from teatree.llm.credentials import ANTHROPIC_BASE_URL_ENV, CredentialError


class _Settings:
    claude_chrome = False
    contribute_plugin_dir = ""


def _agent_exec_argv(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime: str,
    task: str,
) -> tuple[str, list[str]]:
    """Return the executable and argv ``_launch_agent`` would exec."""
    monkeypatch.delenv(ANTHROPIC_BASE_URL_ENV, raising=False)
    with (
        patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch("teatree.config.get_effective_settings", return_value=_Settings()),
        patch("teatree.cli.agent.os.execvp") as execvp_mock,
    ):
        _launch_agent(
            runtime=runtime,
            launch=AgentLaunchContext(
                task=task,
                project_root=Path("/tmp/project"),
                context_lines=["ctx"],
                skills=["t3:code"],
                ask_user_which_skill=False,
            ),
        )
    executable, argv = execvp_mock.call_args.args
    return str(executable), list(argv)


class TestConfiguredCliRuntime:
    @override_settings(TEATREE_INTERACTIVE_RUNTIME="codex", TEATREE_HEADLESS_RUNTIME="claude-code")
    def test_runtime_setting_depends_on_attendance(self) -> None:
        assert _configured_cli_runtime(task="") == "codex"
        assert _configured_cli_runtime(task="do the thing") == "claude-code"

    def test_interactive_codex_uses_native_context_injection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executable, argv = _agent_exec_argv(monkeypatch, runtime="codex", task="")

        assert executable == "/usr/bin/codex"
        assert argv[:3] == ["/usr/bin/codex", "-C", "/tmp/project"]
        config_value = argv[argv.index("-c") + 1]
        assert config_value.startswith("developer_instructions=")
        assert "t3:code" in config_value
        assert "exec" not in argv
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv

    def test_headless_codex_uses_exec_and_unattended_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        executable, argv = _agent_exec_argv(monkeypatch, runtime="codex", task="do the thing")

        assert executable == "/usr/bin/codex"
        assert argv[1] == "exec"
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert argv[-1] == "do the thing"

    def test_unknown_runtime_fails_before_exec(self) -> None:
        with (
            patch("teatree.cli.agent.os.execvp") as execvp_mock,
            pytest.raises(typer.BadParameter, match="Unsupported agent runtime: unknown"),
        ):
            _launch_agent(
                runtime="unknown",
                launch=AgentLaunchContext(
                    task="",
                    project_root=Path("/tmp"),
                    context_lines=["ctx"],
                    skills=[],
                    ask_user_which_skill=False,
                ),
            )

        execvp_mock.assert_not_called()


def _exec_argv(monkeypatch: pytest.MonkeyPatch, *, task: str) -> list[str]:
    """Return the argv ``_launch_claude`` would exec for *task*."""
    monkeypatch.delenv(ANTHROPIC_BASE_URL_ENV, raising=False)
    with (
        patch("shutil.which", return_value="/usr/bin/claude"),
        patch("teatree.config.get_effective_settings", return_value=_Settings()),
        patch("teatree.cli.agent.os.execvp") as execvp_mock,
    ):
        _launch_claude(
            task=task,
            project_root=Path("/tmp"),
            context_lines=["ctx"],
            skills=[],
            ask_user_which_skill=False,
        )
    return list(execvp_mock.call_args.args[1])


class TestUnattendedTaskRunPinsPermissionMode:
    """``t3 agent "<task>"`` execs ``claude -p`` and must pin the mode itself."""

    def test_task_run_pins_the_unattended_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        argv = _exec_argv(monkeypatch, task="fix the sync bug")

        assert "-p" in argv, "a task argument must produce a headless print-mode run"
        flag = argv.index("--permission-mode")
        assert argv[flag + 1] == permission_modes.UNATTENDED

    def test_pinned_mode_matches_the_agent_runner_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Spelled against the shared constant so the two lanes cannot drift apart."""
        argv = _exec_argv(monkeypatch, task="do the thing")

        assert argv[argv.index("--permission-mode") + 1] == _runner_options._PERMISSION_MODE

    def test_bare_interactive_run_pins_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No task means an attended session, whose mode stays the operator's to choose."""
        argv = _exec_argv(monkeypatch, task="")

        assert "-p" not in argv
        assert "--permission-mode" not in argv


class TestUnattendedTaskRunGuardsAmbientBaseUrl:
    """The headless branch refuses a redirected endpoint; the attended branch does not."""

    def test_task_run_refuses_an_ambient_base_url_redirect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ANTHROPIC_BASE_URL_ENV, "https://gateway.example/v1")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.config.get_effective_settings", return_value=_Settings()),
            patch("teatree.cli.agent.os.execvp") as execvp_mock,
            pytest.raises(CredentialError, match=ANTHROPIC_BASE_URL_ENV),
        ):
            _launch_claude(
                task="fix the sync bug",
                project_root=Path("/tmp"),
                context_lines=["ctx"],
                skills=[],
                ask_user_which_skill=False,
            )

        execvp_mock.assert_not_called()

    def test_bare_interactive_run_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The operator is present to see the child's own auth behaviour, so no refusal."""
        monkeypatch.setenv(ANTHROPIC_BASE_URL_ENV, "https://gateway.example/v1")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.config.get_effective_settings", return_value=_Settings()),
            patch("teatree.cli.agent.os.execvp") as execvp_mock,
        ):
            _launch_claude(
                task="",
                project_root=Path("/tmp"),
                context_lines=["ctx"],
                skills=[],
                ask_user_which_skill=False,
            )

        execvp_mock.assert_called_once()
