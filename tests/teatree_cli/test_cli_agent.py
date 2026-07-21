"""``t3 agent`` argv construction — the attended/unattended split on ``-p``.

``t3 agent`` execs two different children off one code path. With no task it
execs an INTERACTIVE ``claude``, which is attended and must inherit the
operator's ``permissions.defaultMode``. With a task it execs ``claude -p``,
which runs headless with nobody present to answer a classifier denial, so it
pins :data:`~teatree.agents.permission_modes.UNATTENDED` and takes the same
ambient base-URL guard as every other seam that spawns an unwatched child.

The pin is what stops ``t3 doctor check``'s own ``auto`` advice from
classifier-gating a headless run.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.agents import _headless_options, permission_modes
from teatree.cli.agent import _launch_claude
from teatree.llm.credentials import ANTHROPIC_BASE_URL_ENV, CredentialError


class _Settings:
    claude_chrome = False
    contribute_plugin_dir = ""


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

    def test_pinned_mode_matches_the_headless_dispatch_lane(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Spelled against the shared constant so the two lanes cannot drift apart."""
        argv = _exec_argv(monkeypatch, task="do the thing")

        assert argv[argv.index("--permission-mode") + 1] == _headless_options._PERMISSION_MODE

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
