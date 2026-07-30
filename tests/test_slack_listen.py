"""Tests for ``t3 slack listen`` and ``t3 slack status`` CLI commands."""

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.slack.listen import _resolve_overlays, slack_app
from teatree.types import RawAPIDict

runner = CliRunner()

_DM_CHANNEL = "D_SELF"
_USER_ID = "U_OPERATOR"


def _seed_registry(db: Path, overlays: dict[str, dict]) -> None:
    """Seed a cold-readable config DB with the ``overlays`` registry row."""
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', "
            "key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'overlays', ?)",
            (json.dumps(overlays),),
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class _RouteAwareFake:
    """Route-aware fake (#1750) recording routed reactions for the egress path."""

    dm_channel_id: str = _DM_CHANNEL
    user_id: str = _USER_ID
    react_routed_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def _is_self_dm(self, channel: str) -> bool:
        return bool(channel) and channel in {self.dm_channel_id, self.user_id}

    def route_token(self, channel: str) -> str:
        return "xoxb-bot" if self._is_self_dm(channel) else "xoxp-user"

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.react_routed_calls.append((channel, ts, emoji))
        return {"ok": True}

    def open_dm(self, user_id: str) -> str:
        return _DM_CHANNEL

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return {"ok": True, "ts": "1700000000.0001"}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return "https://slack.example/p1"


class TestResolveOverlays:
    def test_returns_empty_when_no_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "absent.sqlite3"))
        assert _resolve_overlays("") == []

    def test_reads_overlays_from_registry(self, tmp_path: Path, monkeypatch) -> None:
        db = tmp_path / "config.sqlite3"
        _seed_registry(db, {"myapp": {"messaging_backend": "slack", "slack_token_ref": "test/ref"}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        with patch("teatree.cli.slack.listen.read_pass", side_effect=["xoxb-bot", "xapp-app"]):
            result = _resolve_overlays("")

        assert len(result) == 1
        assert result[0][0] == "myapp"

    def test_skips_non_slack_overlays(self, tmp_path: Path, monkeypatch) -> None:
        db = tmp_path / "config.sqlite3"
        _seed_registry(db, {"myapp": {"messaging_backend": "email"}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        assert _resolve_overlays("") == []

    def test_warns_when_tokens_missing(self, tmp_path: Path, monkeypatch, capsys) -> None:
        db = tmp_path / "config.sqlite3"
        _seed_registry(db, {"broken": {"messaging_backend": "slack", "slack_token_ref": "broken/ref"}})
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        with patch("teatree.cli.slack.listen.read_pass", return_value=""):
            result = _resolve_overlays("")

        assert result == []

    def test_restricts_to_named_overlay(self, tmp_path: Path, monkeypatch) -> None:
        db = tmp_path / "config.sqlite3"
        _seed_registry(
            db,
            {
                "a": {"messaging_backend": "slack", "slack_token_ref": "a/ref"},
                "b": {"messaging_backend": "slack", "slack_token_ref": "b/ref"},
            },
        )
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        with patch("teatree.cli.slack.listen.read_pass", side_effect=["xoxb-bot", "xapp-app"]):
            result = _resolve_overlays("a")

        assert len(result) == 1
        assert result[0][0] == "a"


class TestStatusCommand:
    def test_no_pid_file(self, tmp_path: Path) -> None:
        with patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"):
            result = runner.invoke(slack_app, ["status"])

        assert result.exit_code == 1
        assert "not running" in result.stdout

    def test_stale_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "slack-listener.pid"
        pid_file.write_text("999999999\n", encoding="utf-8")
        with patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"):
            result = runner.invoke(slack_app, ["status"])

        assert result.exit_code == 1
        assert "not running" in result.stdout
        # read_pid reuses the flock file in place; unlinking it would orphan a live holder's lock (#3617).
        assert pid_file.is_file()

    def test_garbled_pid_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "slack-listener.pid"
        pid_file.write_text("not-a-number\n", encoding="utf-8")
        with patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"):
            result = runner.invoke(slack_app, ["status"])

        assert result.exit_code == 1
        assert "not running" in result.stdout
        # read_pid reuses the flock file in place; unlinking it would orphan a live holder's lock (#3617).
        assert pid_file.is_file()

    def test_running_pid(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "slack-listener.pid"
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        with patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"):
            result = runner.invoke(slack_app, ["status"])

        assert result.exit_code == 0
        assert "running" in result.stdout


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestCheckCommand:
    def test_exits_1_when_queue_empty(self) -> None:
        with patch("teatree.backends.slack.receiver.drain_event_queue", return_value=[]):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 1

    def test_stands_down_when_another_drain_holds_the_lock(self) -> None:
        # The 30s cron can double-fire; a concurrent drain would double-ack the
        # same mentions. A singleton serialises it — a second drain stands down
        # (exit 0) and never touches the queue (#3313).
        from teatree.backends.slack.receiver import default_queue_path  # noqa: PLC0415
        from teatree.utils.singleton import singleton  # noqa: PLC0415

        pid = default_queue_path().with_name("slack-drain.pid")
        with (
            singleton("slack-drain", pid_path=pid),
            patch("teatree.backends.slack.receiver.drain_event_queue") as drain,
        ):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        drain.assert_not_called()

    def test_prints_user_messages_as_json(self) -> None:
        events = [
            {
                "overlay": "ov1",
                "event": {"type": "message", "user": "U1", "text": "hello", "ts": "1.0", "channel": _DM_CHANNEL},
            },
            {
                "overlay": "ov1",
                "event": {"type": "app_mention", "user": "U2", "text": "hey @bot", "ts": "2.0", "channel": _DM_CHANNEL},
            },
        ]
        with (
            patch("teatree.backends.slack.receiver.drain_event_queue", return_value=events),
            patch("teatree.cli.slack.listen.messaging_from_overlay", lambda _o=None: _RouteAwareFake()),
        ):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 2

    def test_filters_bot_messages(self) -> None:
        events = [
            {"overlay": "ov", "event": {"type": "message", "bot_id": "B1", "text": "bot msg"}},
            {"overlay": "ov", "event": {"type": "message", "subtype": "bot_message", "text": "sub"}},
            {"overlay": "ov", "event": {"type": "message", "user": "U1", "text": "human", "channel": _DM_CHANNEL}},
        ]
        with (
            patch("teatree.backends.slack.receiver.drain_event_queue", return_value=events),
            patch("teatree.cli.slack.listen.messaging_from_overlay", lambda _o=None: _RouteAwareFake()),
        ):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1
        assert "human" in lines[0]

    def test_ack_on_user_own_dm_stays_ungated(self) -> None:
        """The :eyes: ack on the user's OWN inbound DM stays ungated (self branch)."""
        events = [
            {
                "overlay": "ov",
                "event": {"type": "message", "user": "U1", "text": "hi", "ts": "1.0", "channel": _DM_CHANNEL},
            },
        ]
        fake = _RouteAwareFake()
        with (
            patch("teatree.backends.slack.receiver.drain_event_queue", return_value=events),
            patch("teatree.cli.slack.listen.messaging_from_overlay", lambda _o=None: fake),
        ):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        assert fake.react_routed_calls == [(_DM_CHANNEL, "1.0", "eyes")]

    def test_ack_skips_when_no_backend(self) -> None:
        """No slack backend for the overlay → skip the reaction, do not crash."""
        events = [{"overlay": "ov", "event": {"type": "message", "user": "U1", "text": "hi", "ts": "1.0"}}]
        with (
            patch("teatree.backends.slack.receiver.drain_event_queue", return_value=events),
            patch("teatree.cli.slack.listen.messaging_from_overlay", lambda _o=None: None),
        ):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0

    def test_commits_backing_file_after_ack(self, tmp_path: Path, monkeypatch) -> None:
        """The drained file is discarded only after acking the messages."""
        import json  # noqa: PLC0415

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        queue = tmp_path / "teatree" / "slack-events.jsonl"
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text(
            json.dumps(
                {
                    "overlay": "ov",
                    "event": {"type": "message", "user": "U1", "text": "hi", "ts": "1.0", "channel": _DM_CHANNEL},
                },
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("teatree.cli.slack.listen.messaging_from_overlay", lambda _o=None: _RouteAwareFake()):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        assert not queue.is_file()
        assert not queue.with_suffix(".draining").is_file()

    def test_empty_queue_commits_so_bot_events_do_not_replay(self, tmp_path: Path, monkeypatch) -> None:
        """Bot-only events still discard the backing file (no infinite replay)."""
        import json  # noqa: PLC0415

        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        queue = tmp_path / "teatree" / "slack-events.jsonl"
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.write_text(
            json.dumps({"overlay": "ov", "event": {"type": "message", "bot_id": "B1", "text": "bot"}}) + "\n",
            encoding="utf-8",
        )

        result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 1
        assert not queue.with_suffix(".draining").is_file()


class TestListenCommand:
    def test_exits_when_no_overlays(self, tmp_path: Path) -> None:
        with (
            patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch("teatree.cli.slack.listen._resolve_overlays", return_value=[]),
        ):
            result = runner.invoke(slack_app, ["listen"])

        assert result.exit_code == 1
        assert "No slack-enabled overlays" in result.stdout

    def test_exits_when_already_running(self, tmp_path: Path) -> None:
        from teatree.utils.singleton import singleton  # noqa: PLC0415

        pid_file = tmp_path / "slack-listener.pid"
        with (
            singleton("slack-listener", pid_path=pid_file),
            patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
        ):
            result = runner.invoke(slack_app, ["listen"])

        assert result.exit_code == 1
        assert "already running" in result.stdout

    def test_replaces_stale_pid_and_proceeds(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "slack-listener.pid"
        pid_file.write_text("999999999\n", encoding="utf-8")
        with (
            patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch("teatree.cli.slack.listen._resolve_overlays", return_value=[("ov", "xapp", "xoxb")]),
            patch("teatree.cli.slack.listen.run_listener"),
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
            patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch("teatree.cli.slack.listen._resolve_overlays", return_value=[("ov", "xapp", "xoxb")]),
            patch("teatree.cli.slack.listen.run_listener"),
        ):
            runner.invoke(slack_app, ["listen"])

        pid_file = tmp_path / "slack-listener.pid"
        with singleton("slack-listener", pid_path=pid_file) as held:
            assert held == pid_file

    def test_lock_released_on_exception(self, tmp_path: Path) -> None:
        from teatree.utils.singleton import singleton  # noqa: PLC0415

        with (
            patch("teatree.cli.slack.listen.default_queue_path", return_value=tmp_path / "events.jsonl"),
            patch("teatree.cli.slack.listen._resolve_overlays", return_value=[("ov", "xapp", "xoxb")]),
            patch("teatree.cli.slack.listen.run_listener", side_effect=RuntimeError("boom")),
        ):
            result = runner.invoke(slack_app, ["listen"])

        assert result.exit_code != 0
        pid_file = tmp_path / "slack-listener.pid"
        with singleton("slack-listener", pid_path=pid_file) as held:
            assert held == pid_file


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestReactCommand:
    """``t3 slack react`` routes through the on-behalf egress (#960/#1750)."""

    def _gate(self, tmp_path: Path, monkeypatch, mode: str) -> None:
        from teatree.core.models import ConfigSetting  # noqa: PLC0415

        ConfigSetting.objects.set_value("slack_user_id", _USER_ID)
        ConfigSetting.objects.set_value("on_behalf_post_mode", mode)
        monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda _o=None: _RouteAwareFake())

    def test_no_backend_exits_1(self, tmp_path: Path, monkeypatch) -> None:
        self._gate(tmp_path, monkeypatch, "immediate")
        with patch("teatree.cli.slack.listen.messaging_from_overlay", lambda _o=None: None):
            result = runner.invoke(slack_app, ["react", "D1", "1.0", "eyes"])

        assert result.exit_code == 1
        assert "No slack backend" in result.stdout

    def test_self_dm_react_succeeds_ungated(self, tmp_path: Path, monkeypatch) -> None:
        self._gate(tmp_path, monkeypatch, "ask")
        fake = _RouteAwareFake()
        with patch("teatree.cli.slack.listen.messaging_from_overlay", lambda _o=None: fake):
            result = runner.invoke(slack_app, ["react", _DM_CHANNEL, "1.0", "eyes"])

        assert result.exit_code == 0
        assert "Reacted :eyes:" in result.stdout
        assert fake.react_routed_calls == [(_DM_CHANNEL, "1.0", "eyes")]

    def test_colleague_react_blocked_under_ask(self, tmp_path: Path, monkeypatch) -> None:
        self._gate(tmp_path, monkeypatch, "ask")
        fake = _RouteAwareFake()
        with patch("teatree.cli.slack.listen.messaging_from_overlay", lambda _o=None: fake):
            result = runner.invoke(slack_app, ["react", "C_COLLEAGUE", "1.0", "merge"])

        assert result.exit_code == 1
        assert "approve-on-behalf" in result.stdout
        assert fake.react_routed_calls == []
