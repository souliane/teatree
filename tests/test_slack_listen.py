"""Tests for ``t3 slack listen`` and ``t3 slack status`` CLI commands."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from teatree.cli.slack_listen import _resolve_overlays, post_reaction, slack_app

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
        with (
            patch("teatree.backends.slack_receiver.drain_event_queue", return_value=events),
            patch("teatree.cli.slack_listen._resolve_reaction_token", return_value="xoxp-test"),
            patch("teatree.cli.slack_listen.post_reaction", return_value=True),
        ):
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
        with (
            patch("teatree.backends.slack_receiver.drain_event_queue", return_value=events),
            patch("teatree.cli.slack_listen._resolve_reaction_token", return_value="xoxp-test"),
            patch("teatree.cli.slack_listen.post_reaction", return_value=True),
        ):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        lines = result.stdout.strip().splitlines()
        assert len(lines) == 1
        assert "human" in lines[0]

    def test_ack_uses_personal_user_token_not_bot(self) -> None:
        """``_ack_messages`` reads ``pass slack/user-oauth-token`` (#1232).

        The bot token cannot ``reactions.add`` on user DMs
        (``message_not_found``) or Slack-Connect channels
        (``mcp_externally_shared_channel_restricted``); the personal
        ``xoxp-…`` token is the only credential that reliably reaches both.
        """
        events = [{"overlay": "ov", "event": {"type": "message", "user": "U1", "text": "hi", "ts": "1.0"}}]
        captured: dict[str, str] = {}

        def _spy(*, token: str, channel: str, ts: str, emoji: str) -> bool:
            captured.update(token=token, channel=channel, ts=ts, emoji=emoji)
            return True

        with (
            patch("teatree.backends.slack_receiver.drain_event_queue", return_value=events),
            patch("teatree.cli.slack_listen._resolve_reaction_token", return_value="xoxp-personal-abc"),
            patch("teatree.cli.slack_listen.post_reaction", side_effect=_spy),
        ):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        assert captured["token"] == "xoxp-personal-abc"
        assert captured["emoji"] == "eyes"

    def test_ack_warns_and_skips_when_user_token_missing(self) -> None:
        """No personal token → warn and skip the reaction (do not crash)."""
        events = [{"overlay": "ov", "event": {"type": "message", "user": "U1", "text": "hi", "ts": "1.0"}}]
        with (
            patch("teatree.backends.slack_receiver.drain_event_queue", return_value=events),
            patch("teatree.cli.slack_listen._resolve_reaction_token", return_value=""),
            patch("teatree.cli.slack_listen.post_reaction") as posted,
        ):
            result = runner.invoke(slack_app, ["check"])

        assert result.exit_code == 0
        posted.assert_not_called()


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


class TestPostReaction:
    """Direct tests of the ``post_reaction`` helper used by ``react`` / ``check`` (#1232)."""

    def _response(self, *, status: int = 200, payload: dict[str, object] | None = None) -> MagicMock:
        response = MagicMock(spec=httpx.Response)
        response.is_success = status < 400
        response.status_code = status
        response.json.return_value = payload or {"ok": True}
        return response

    def test_empty_arg_returns_false(self) -> None:
        assert post_reaction(token="", channel="C1", ts="1.0", emoji="eyes") is False
        assert post_reaction(token="xoxp-x", channel="", ts="1.0", emoji="eyes") is False
        assert post_reaction(token="xoxp-x", channel="C1", ts="", emoji="eyes") is False
        assert post_reaction(token="xoxp-x", channel="C1", ts="1.0", emoji="") is False

    def test_ok_response_is_success(self) -> None:
        with patch("teatree.cli.slack_listen.httpx.post", return_value=self._response()) as post:
            assert post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes") is True
        post.assert_called_once()
        kwargs = post.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer xoxp-1"
        assert kwargs["json"] == {"channel": "D1", "timestamp": "1.0", "name": "eyes"}

    def test_already_reacted_is_success(self) -> None:
        payload = {"ok": False, "error": "already_reacted"}
        with patch("teatree.cli.slack_listen.httpx.post", return_value=self._response(payload=payload)):
            assert post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes") is True

    def test_missing_scope_is_failure(self) -> None:
        payload = {"ok": False, "error": "missing_scope"}
        with patch("teatree.cli.slack_listen.httpx.post", return_value=self._response(payload=payload)):
            assert post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes") is False

    def test_transport_error_is_failure(self) -> None:
        with patch("teatree.cli.slack_listen.httpx.post", side_effect=httpx.ConnectError("nope")):
            assert post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes") is False

    def test_non_2xx_is_failure(self) -> None:
        with patch("teatree.cli.slack_listen.httpx.post", return_value=self._response(status=500)):
            assert post_reaction(token="xoxp-1", channel="D1", ts="1.0", emoji="eyes") is False


class TestReactCommand:
    """``t3 slack react`` CLI surface (#1232)."""

    def test_missing_user_token_exits_1(self) -> None:
        with patch("teatree.cli.slack_listen._resolve_reaction_token", return_value=""):
            result = runner.invoke(slack_app, ["react", "D1", "1.0", "eyes"])

        assert result.exit_code == 1
        assert "Run `t3 setup slack-user-token`" in result.stdout

    def test_success_exits_0(self) -> None:
        with (
            patch("teatree.cli.slack_listen._resolve_reaction_token", return_value="xoxp-abc"),
            patch("teatree.cli.slack_listen.post_reaction", return_value=True) as post,
        ):
            result = runner.invoke(slack_app, ["react", "D1", "1.0", "eyes"])

        assert result.exit_code == 0
        assert "Reacted :eyes:" in result.stdout
        post.assert_called_once_with(token="xoxp-abc", channel="D1", ts="1.0", emoji="eyes")

    def test_api_failure_exits_2(self) -> None:
        with (
            patch("teatree.cli.slack_listen._resolve_reaction_token", return_value="xoxp-abc"),
            patch("teatree.cli.slack_listen.post_reaction", return_value=False),
        ):
            result = runner.invoke(slack_app, ["react", "D1", "1.0", "eyes"])

        assert result.exit_code == 2
        assert "reactions.add failed" in result.stdout


class TestPostReactionLive:
    """Integration test that fires a real ``reactions.add`` if creds + target are configured.

    Skipped by default so CI stays hermetic. Set ``T3_SLACK_REACT_TEST_CHANNEL``
    and ``T3_SLACK_REACT_TEST_TS`` to a channel+ts the personal token at
    ``pass slack/user-oauth-token`` can write to (typically a self-DM scratch
    message), then ``uv run pytest tests/test_slack_listen.py::TestPostReactionLive``.
    The test passes when ``post_reaction`` returns True (idempotent ``already_reacted``
    is treated as success), proving the ``reactions:write`` scope is actually granted.
    """

    @pytest.mark.skipif(
        not (os.environ.get("T3_SLACK_REACT_TEST_CHANNEL") and os.environ.get("T3_SLACK_REACT_TEST_TS")),
        reason="Set T3_SLACK_REACT_TEST_CHANNEL and T3_SLACK_REACT_TEST_TS to exercise the live path.",
    )
    def test_reacts_with_personal_token(self) -> None:
        from teatree.cli.slack_listen import _resolve_reaction_token  # noqa: PLC0415

        token = _resolve_reaction_token()
        if not token:
            pytest.skip("No personal user-OAuth token in pass — run `t3 setup slack-user-token` first.")
        channel = os.environ["T3_SLACK_REACT_TEST_CHANNEL"]
        ts = os.environ["T3_SLACK_REACT_TEST_TS"]
        assert post_reaction(token=token, channel=channel, ts=ts, emoji="white_check_mark") is True
