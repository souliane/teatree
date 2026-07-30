"""Tests for ``teatree.cli.slack.channel_provisioning`` — bot review-channel join (#1686)."""

from unittest.mock import MagicMock

from teatree.backends.slack.bot import SlackBotBackend
from teatree.cli.slack.channel_provisioning import JoinStatus, join_channel, join_review_channels, render_join_result


def _backend(join_body: dict) -> MagicMock:
    backend = MagicMock(spec=SlackBotBackend)
    backend.join_conversation.return_value = join_body
    return backend


class TestJoinChannel:
    def test_fresh_join(self) -> None:
        result = join_channel(backend=_backend({"ok": True}), channel_id="C1", channel_name="rev")
        assert result.status is JoinStatus.JOINED

    def test_already_in_channel(self) -> None:
        result = join_channel(backend=_backend({"ok": True, "already_in_channel": True}), channel_id="C1")
        assert result.status is JoinStatus.ALREADY_IN

    def test_connect_channel_needs_manual_invite(self) -> None:
        backend = _backend({"ok": False, "error": "method_not_supported_for_channel_type"})
        result = join_channel(backend=backend, channel_id="C1", channel_name="connect")
        assert result.status is JoinStatus.NEEDS_MANUAL_INVITE
        assert result.detail == "method_not_supported_for_channel_type"

    def test_missing_scope_needs_manual_invite(self) -> None:
        result = join_channel(backend=_backend({"ok": False, "error": "missing_scope"}), channel_id="C1")
        assert result.status is JoinStatus.NEEDS_MANUAL_INVITE

    def test_unknown_error_is_failed(self) -> None:
        result = join_channel(backend=_backend({"ok": False, "error": "ratelimited"}), channel_id="C1")
        assert result.status is JoinStatus.FAILED
        assert result.detail == "ratelimited"


class TestJoinReviewChannels:
    def test_skips_empty_ids(self) -> None:
        backend = _backend({"ok": True})
        results = join_review_channels(backend=backend, channels=[("a", ""), ("b", "C2")])
        assert len(results) == 1
        assert results[0].channel_id == "C2"

    def test_joins_each(self) -> None:
        backend = _backend({"ok": True})
        results = join_review_channels(backend=backend, channels=[("a", "C1"), ("b", "C2")])
        assert [r.channel_id for r in results] == ["C1", "C2"]
        assert backend.join_conversation.call_count == 2


class TestRenderJoinResult:
    def test_manual_invite_is_actionable(self) -> None:
        lines: list[str] = []
        result = join_channel(
            backend=_backend({"ok": False, "error": "method_not_supported_for_channel_type"}),
            channel_id="C1",
            channel_name="connect",
        )
        render_join_result(result, lines.append)
        assert any("ACTION" in line and "connect" in line for line in lines)

    def test_joined_is_ok(self) -> None:
        lines: list[str] = []
        render_join_result(join_channel(backend=_backend({"ok": True}), channel_id="C1"), lines.append)
        assert any("OK" in line for line in lines)

    def test_already_in_renders_ok(self) -> None:
        lines: list[str] = []
        result = join_channel(backend=_backend({"ok": True, "already_in_channel": True}), channel_id="C1")
        render_join_result(result, lines.append)
        assert any("already in" in line for line in lines)

    def test_failed_renders_warn(self) -> None:
        lines: list[str] = []
        result = join_channel(backend=_backend({"ok": False, "error": "ratelimited"}), channel_id="C1")
        render_join_result(result, lines.append)
        assert any("WARN" in line and "ratelimited" in line for line in lines)
