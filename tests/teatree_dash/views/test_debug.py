"""Debug-access views: allowlisted command buttons + loopback ttyd session (#3162)."""

from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse

from teatree.agents.terminal_launcher import LaunchResult
from teatree.dash.commands import CommandBusyError, CommandResult


class CommandRunViewTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:command_run")

    def test_non_allowlisted_command_rejected(self) -> None:
        resp = self.client.post(self.url, {"command": "rm-rf-slash"})
        assert resp.status_code == 400

    def test_allowlisted_command_runs_and_renders_output(self) -> None:
        fake = CommandResult(
            key="doctor",
            label="t3 doctor check",
            argv=("t3", "doctor", "check"),
            exit_code=0,
            output="all green",
            timed_out=False,
        )
        with patch("teatree.dash.views.debug.run_allowlisted", return_value=fake) as run:
            resp = self.client.post(self.url, {"command": "doctor"})
        run.assert_called_once_with("doctor", loop_name="")
        assert resp.status_code == 200
        assert "all green" in resp.content.decode()

    def test_command_is_audited(self) -> None:
        fake = CommandResult(
            key="doctor",
            label="t3 doctor check",
            argv=("t3", "doctor", "check"),
            exit_code=0,
            output="",
            timed_out=False,
        )
        with (
            patch("teatree.dash.views.debug.run_allowlisted", return_value=fake),
            self.assertLogs("teatree.dash.audit", level="INFO") as logs,
        ):
            self.client.post(self.url, {"command": "doctor"})
        assert any("action=command:doctor" in line for line in logs.output)

    def test_busy_command_returns_429(self) -> None:
        # DASH-6: a run rejected because the same command (or the cap) is in flight
        # is a transient busy-signal — surfaced as 429, not stacked onto a worker thread.
        with patch(
            "teatree.dash.views.debug.run_allowlisted",
            side_effect=CommandBusyError("command 'doctor' is already running — wait for it to finish"),
        ):
            resp = self.client.post(self.url, {"command": "doctor"})
        assert resp.status_code == 429
        assert "already running" in resp.content.decode()

    def test_csrf_enforced(self) -> None:
        csrf_client = Client(enforce_csrf_checks=True)
        resp = csrf_client.post(self.url, {"command": "doctor"})
        assert resp.status_code == 403


class DebugSessionViewTestCase(TestCase):
    def setUp(self) -> None:
        self.url = reverse("dash:debug_session")

    def test_launches_and_renders_loopback_url(self) -> None:
        with patch(
            "teatree.dash.views.debug.launch_web_session",
            return_value=LaunchResult(launch_url="http://127.0.0.1:54321", pid=42),
        ) as launch:
            resp = self.client.post(self.url, {})
        launch.assert_called_once_with("")
        assert "http://127.0.0.1:54321" in resp.content.decode()

    def test_missing_claude_returns_400(self) -> None:
        with patch(
            "teatree.dash.views.debug.launch_web_session", side_effect=FileNotFoundError("claude CLI is not installed")
        ):
            resp = self.client.post(self.url, {})
        assert resp.status_code == 400
