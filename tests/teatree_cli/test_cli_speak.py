"""Tests for the top-level ``t3 speak`` CLI command (delegates to management command)."""

from contextlib import AbstractContextManager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import typer
from typer.testing import CliRunner

from teatree.cli.speak import speak
from teatree.config.agent_enums import AgentRuntime

runner = CliRunner()

_app = typer.Typer()
_app.command()(speak)


def _patch_runtime(runtime: AgentRuntime) -> AbstractContextManager[MagicMock]:
    return patch("teatree.config.get_effective_settings", return_value=SimpleNamespace(agent_runtime=runtime))


class TestSpeakCommandDelegation:
    def test_delegates_to_management_command(self) -> None:
        with (
            patch("django.setup"),
            _patch_runtime(AgentRuntime.INTERACTIVE),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["tests are green"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("speak", "tests are green", overlay="")

    def test_passes_overlay_flag(self) -> None:
        with (
            patch("django.setup"),
            _patch_runtime(AgentRuntime.INTERACTIVE),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["hi", "--overlay", "teatree"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("speak", "hi", overlay="teatree")


class TestSpeakRefusesUnderHeadlessRuntime:
    """``t3 speak`` is a local-audio-only sink refused under a headless runtime.

    A headless agent shelling out to it would lose the message (it never reaches an
    away user), so the command is a no-op-with-warning and can't be a silent
    lost-contact vector — the sanctioned path is the Slack DeferredQuestion egress.
    """

    def test_headless_runtime_is_a_no_op_with_warning(self) -> None:
        with (
            patch("django.setup"),
            _patch_runtime(AgentRuntime.HEADLESS),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["hello away user"])
        assert result.exit_code == 0
        call_mock.assert_not_called()
        assert "headless" in result.output.lower()

    def test_settings_read_failure_falls_open_and_still_speaks(self) -> None:
        # A settings-read failure must never silence a present user's read — fail open.
        with (
            patch("django.setup"),
            patch("teatree.config.get_effective_settings", side_effect=RuntimeError("no db")),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["still speak"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("speak", "still speak", overlay="")
