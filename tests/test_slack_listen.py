"""Tests for ``t3 slack listen`` and ``t3 slack status`` CLI commands."""

import os
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.slack_listen import _resolve_overlays, slack_app

runner = CliRunner()


class TestResolveOverlays:
    def test_returns_empty_when_no_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr("teatree.cli.slack_listen.Path.home", lambda: tmp_path)
        assert _resolve_overlays("") == []

    def test_reads_overlays_from_toml(self, tmp_path: Path, monkeypatch) -> None:
        config = tmp_path / ".teatree.toml"
        config.write_text(
            '[overlays.myapp]\nmessaging_backend = "slack"\nslack_token_ref = "test/ref"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.cli.slack_listen.Path.home", lambda: tmp_path)
        with patch("teatree.cli.slack_listen.read_pass", side_effect=["xoxb-bot", "xapp-app"]):
            result = _resolve_overlays("")

        assert len(result) == 1
        assert result[0][0] == "myapp"

    def test_skips_non_slack_overlays(self, tmp_path: Path, monkeypatch) -> None:
        config = tmp_path / ".teatree.toml"
        config.write_text(
            '[overlays.myapp]\nmessaging_backend = "email"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.cli.slack_listen.Path.home", lambda: tmp_path)
        assert _resolve_overlays("") == []

    def test_warns_when_tokens_missing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        config = tmp_path / ".teatree.toml"
        config.write_text(
            '[overlays.broken]\nmessaging_backend = "slack"\nslack_token_ref = "broken/ref"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.cli.slack_listen.Path.home", lambda: tmp_path)
        with patch("teatree.cli.slack_listen.read_pass", return_value=""):
            result = _resolve_overlays("")

        assert result == []

    def test_restricts_to_named_overlay(self, tmp_path: Path, monkeypatch) -> None:
        config = tmp_path / ".teatree.toml"
        config.write_text(
            '[overlays.a]\nmessaging_backend = "slack"\nslack_token_ref = "a/ref"\n'
            '[overlays.b]\nmessaging_backend = "slack"\nslack_token_ref = "b/ref"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.cli.slack_listen.Path.home", lambda: tmp_path)
        with patch("teatree.cli.slack_listen.read_pass", side_effect=["xoxb-bot", "xapp-app"]):
            result = _resolve_overlays("a")

        assert len(result) == 1
        assert result[0][0] == "a"


class TestStatusCommand:
    def test_no_pid_file(self, tmp_path: Path) -> None:
        with patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"):
            result = runner.invoke(slack_app, ["status"])

        assert result.exit_code == 1
        assert "not running" in result.stdout

    def test_stale_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "slack-listener.pid"
        pid_file.write_text("999999999\n", encoding="utf-8")
        with patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"):
            result = runner.invoke(slack_app, ["status"])

        assert result.exit_code == 1
        assert "not running" in result.stdout
        assert not pid_file.is_file()

    def test_garbled_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "slack-listener.pid"
        pid_file.write_text("not-a-number\n", encoding="utf-8")
        with patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"):
            result = runner.invoke(slack_app, ["status"])

        assert result.exit_code == 1
        assert "not running" in result.stdout
        assert not pid_file.is_file()

    def test_running_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "slack-listener.pid"
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        with patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"):
            result = runner.invoke(slack_app, ["status"])

        assert result.exit_code == 0
        assert "running" in result.stdout


class TestCheckCommand:
    def test_exits_1_when_queue_empty(self) -> None:
        with patch("teatree.backends.slack_receiver.drain_event_queue", return_value=[]):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 1

    def test_prints_user_messages_as_json(self) -> None:
        events = [
            {"overlay": "ov1", "event": {"type": "message", "user": "U1", "text": "hello", "ts": "1.0"}},
            {"overlay": "ov1", "event": {"type": "app_mention", "user": "U2", "text": "hey @bot", "ts": "2.0"}},
        ]
        with patch("teatree.backends.slack_receiver.drain_event_queue", return_value=events):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 2

    def test_filters_bot_messages(self) -> None:
        events = [
            {"overlay": "ov", "event": {"type": "message", "bot_id": "B1", "text": "bot msg"}},
            {"overlay": "ov", "event": {"type": "message", "subtype": "bot_message", "text": "sub"}},
            {"overlay": "ov", "event": {"type": "message", "user": "U1", "text": "human"}},
        ]
        with patch("teatree.backends.slack_receiver.drain_event_queue", return_value=events):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1
        assert "human" in lines[0]


class TestListenCommand:
    def test_exits_when_no_overlays(self, tmp_path: Path) -> None:
        with (
            patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch("teatree.cli.slack_listen._resolve_overlays", return_value=[]),
        ):
            result = runner.invoke(slack_app, ["listen"])

        assert result.exit_code == 1
        assert "No slack-enabled overlays" in result.stdout

    def test_exits_when_already_running(self, tmp_path: Path) -> None:
        from teatree.utils.singleton import singleton  # noqa: PLC0415

        pid_file = tmp_path / "slack-listener.pid"
        with (
            singleton("slack-listener", pid_path=pid_file),
            patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
        ):
            result = runner.invoke(slack_app, ["listen"])

        assert result.exit_code == 1
        assert "already running" in result.stdout

    def test_replaces_stale_pid_and_proceeds(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "slack-listener.pid"
        pid_file.write_text("999999999\n", encoding="utf-8")
        with (
            patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch("teatree.cli.slack_listen._resolve_overlays", return_value=[("ov", "xapp", "xoxb")]),
            patch("teatree.cli.slack_listen.run_listener"),
        ):
            result = runner.invoke(slack_app, ["listen"])

        assert result.exit_code == 0
        assert "Listening on ov" in result.stdout

    def test_lock_released_after_run(self, tmp_path: Path) -> None:
        """The flock releases on clean exit so the lock is re-acquirable.

        The lock file itself persists by design (unlinking a path another
        opener may already hold reintroduces a double-acquire race — see
        ``teatree.utils.singleton``); release is proven by re-acquiring,
        not by file absence.
        """
        from teatree.utils.singleton import singleton  # noqa: PLC0415

        with (
            patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch("teatree.cli.slack_listen._resolve_overlays", return_value=[("ov", "xapp", "xoxb")]),
            patch("teatree.cli.slack_listen.run_listener"),
        ):
            runner.invoke(slack_app, ["listen"])

        pid_file = tmp_path / "slack-listener.pid"
        with singleton("slack-listener", pid_path=pid_file) as held:
            assert held == pid_file

    def test_lock_released_on_exception(self, tmp_path: Path) -> None:
        from teatree.utils.singleton import singleton  # noqa: PLC0415

        with (
            patch("teatree.cli.slack_listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch("teatree.cli.slack_listen._resolve_overlays", return_value=[("ov", "xapp", "xoxb")]),
            patch("teatree.cli.slack_listen.run_listener", side_effect=RuntimeError("boom")),
        ):
            result = runner.invoke(slack_app, ["listen"])

        assert result.exit_code != 0
        pid_file = tmp_path / "slack-listener.pid"
        with singleton("slack-listener", pid_path=pid_file) as held:
            assert held == pid_file
