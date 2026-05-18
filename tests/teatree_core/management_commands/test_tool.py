"""Tests for the tool management command."""

import subprocess
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.utils.run as utils_run_mod
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, MINIMAL_OVERLAY, SETTINGS, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


# ── Tool commands ──────────────────────────────────────────────────


class TestToolList(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_shows_available_tools(self) -> None:
        result = cast("str", call_command("tool", "list"))

        assert "migrate: Run DB migrations" in result
        assert "seed: Seed test data" in result
        assert "broken" in result

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_tools(self) -> None:
        result = cast("str", call_command("tool", "list"))

        assert "no tool commands" in result.lower()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_tools_without_help(self) -> None:
        """Tools without a help string show just the name."""
        helpless_overlay = "tests.teatree_core.management_commands._overlays.HelplessToolOverlay"
        with _patch_overlays(helpless_overlay):
            result = cast("str", call_command("tool", "list"))

        assert "bare-tool" in result


class TestToolRun(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_executes_command(self) -> None:
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            result = cast("str", call_command("tool", "run", "migrate"))

        assert "completed" in result.lower()
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["echo", "migrate"]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_unknown_tool_raises_system_exit_1(self) -> None:
        """An unknown tool name is a usage error — exit 1, not 0.

        Regression for #932: `t3 <ov> tool run <typo>` returned a string and
        exited 0, so a scripted caller never noticed the tool never ran.
        """
        with pytest.raises(SystemExit) as exc_info:
            call_command("tool", "run", "nonexistent")

        assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_command_raises_system_exit_1(self) -> None:
        """A configured tool with no command is a misconfig — exit 1."""
        with pytest.raises(SystemExit) as exc_info:
            call_command("tool", "run", "broken")

        assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_forwards_extra_args(self) -> None:
        """Extra args after the tool name are appended to the command."""
        with patch.object(utils_run_mod.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            result = cast(
                "str",
                call_command("tool", "run", "migrate", "--verbose", "--dry-run"),
            )

        assert "completed" in result.lower()
        cmd = mock_run.call_args[0][0]
        assert cmd == ["echo", "migrate", "--verbose", "--dry-run"]
