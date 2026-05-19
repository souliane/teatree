"""Tests for ``handle_inject_pending_chat`` UserPromptSubmit hook (#1014).

The drain side of the Slack inbound bridge — WS2 / WS4. The handler
reads unconsumed :class:`PendingChatInjection` rows for the loop-owner
session and writes them into the next user turn's ``additionalContext``
so a Slack DM reaches the agent as if the user had typed it.

Gated on ``_session_owns_loop`` — a non-owner session never drains the
queue, matching the §5.6 ``handle_loop_self_pump`` discipline so a
fresh side-session does not steal the user's reply intended for the
loop owner.
"""

import os
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _OWNER_LOOP, _write_loop_registry, handle_inject_pending_chat
from teatree.core.models import PendingChatInjection

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)


def _own_loop(session_id: str) -> None:
    _write_loop_registry(
        {
            _OWNER_LOOP: {
                "session_id": session_id,
                "agent_id": "a",
                "pid": os.getpid(),
                "heartbeat_ts": int(time.time()),
            }
        }
    )


class TestDrainAsOwner:
    def test_owner_session_drains_one_pending_row(self, capsys: pytest.CaptureFixture[str]) -> None:
        _own_loop("owner-1")
        PendingChatInjection.record(
            channel="D0B36P8LU86",
            slack_ts="1700000000.0001",
            text="Please review PR #42",
            user_id="U0A72P7CK0A",
        )

        handle_inject_pending_chat({"session_id": "owner-1"})

        out = capsys.readouterr().out
        assert "Please review PR #42" in out
        assert "Slack" in out
        row = PendingChatInjection.objects.get()
        assert row.consumed_at is not None
        assert row.is_pending is False

    def test_drains_multiple_rows_oldest_first(self, capsys: pytest.CaptureFixture[str]) -> None:
        _own_loop("owner-2")
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="first")
        PendingChatInjection.record(channel="D", slack_ts="2.0", text="second")

        handle_inject_pending_chat({"session_id": "owner-2"})

        out = capsys.readouterr().out
        first_pos = out.find("first")
        second_pos = out.find("second")
        assert first_pos != -1
        assert second_pos != -1
        assert first_pos < second_pos
        assert PendingChatInjection.objects.filter(consumed_at__isnull=True).count() == 0

    def test_already_consumed_rows_are_ignored(self, capsys: pytest.CaptureFixture[str]) -> None:
        _own_loop("owner-3")
        old = PendingChatInjection.record(channel="D", slack_ts="1.0", text="old message")
        assert old is not None
        old.consume()
        PendingChatInjection.record(channel="D", slack_ts="2.0", text="fresh message")

        handle_inject_pending_chat({"session_id": "owner-3"})

        out = capsys.readouterr().out
        assert "old message" not in out
        assert "fresh message" in out

    def test_re_firing_handler_on_consumed_rows_is_no_op(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Idempotency: a second invocation sees the queue empty and emits nothing."""
        _own_loop("owner-4")
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="ping")

        handle_inject_pending_chat({"session_id": "owner-4"})
        capsys.readouterr()  # drain

        handle_inject_pending_chat({"session_id": "owner-4"})

        out = capsys.readouterr().out
        assert out == ""

    def test_empty_queue_emits_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        _own_loop("owner-5")

        handle_inject_pending_chat({"session_id": "owner-5"})

        assert capsys.readouterr().out == ""


class TestOwnershipGate:
    def test_non_owner_session_does_not_drain(self, capsys: pytest.CaptureFixture[str]) -> None:
        _own_loop("the-owner")
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="message for owner")

        handle_inject_pending_chat({"session_id": "some-other-session"})

        out = capsys.readouterr().out
        assert "message for owner" not in out
        row = PendingChatInjection.objects.get()
        assert row.consumed_at is None

    def test_no_owner_registered_does_not_drain(self, capsys: pytest.CaptureFixture[str]) -> None:
        """If no session owns the loop, no session may drain the queue."""
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="message")

        handle_inject_pending_chat({"session_id": "any"})

        out = capsys.readouterr().out
        assert "message" not in out
        assert PendingChatInjection.objects.get().is_pending is True

    def test_missing_session_id_is_no_op(self, capsys: pytest.CaptureFixture[str]) -> None:
        _own_loop("owner")
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="msg")

        handle_inject_pending_chat({})

        assert capsys.readouterr().out == ""
        assert PendingChatInjection.objects.get().is_pending is True


class TestRouterWiring:
    def test_inject_pending_chat_registered_for_user_prompt_submit(self) -> None:
        handlers = router._HANDLERS["UserPromptSubmit"]
        names = [h.__name__ for h in handlers]
        assert "handle_inject_pending_chat" in names

    def test_inject_pending_chat_runs_before_user_prompt_submit_record(self) -> None:
        """Drain order: chat-injection drain precedes the prompt recorder.

        The injected content must participate in the same turn the
        recorder logs, so the order is load-bearing — not cosmetic.
        """
        handlers = router._HANDLERS["UserPromptSubmit"]
        names = [h.__name__ for h in handlers]
        assert names.index("handle_inject_pending_chat") < names.index("handle_user_prompt_submit")


class TestFormat:
    def test_each_row_renders_as_a_user_said_via_slack_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        _own_loop("owner")
        PendingChatInjection.record(
            channel="D",
            slack_ts="1700000000.0001",
            text="merge PR #42",
            user_id="U0A72P7CK0A",
        )

        handle_inject_pending_chat({"session_id": "owner"})

        out = capsys.readouterr().out
        # Format: ``User replied on Slack at <ts>: <text>``
        assert "Slack" in out
        assert "1700000000.0001" in out
        assert "merge PR #42" in out


class TestE2ESlackToInjection:
    """End-to-end no-network: scanner emits → handler drains (#1014 WS4)."""

    def test_scan_then_drain_round_trip(self, capsys: pytest.CaptureFixture[str]) -> None:
        from dataclasses import dataclass, field  # noqa: PLC0415

        from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner  # noqa: PLC0415
        from teatree.types import RawAPIDict  # noqa: PLC0415

        @dataclass
        class FakeBackend:
            dms: list[RawAPIDict] = field(default_factory=list)

            def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
                _ = since
                return []

            def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
                _ = since
                return self.dms

            def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
                _ = (channel, text, thread_ts)
                return {}

            def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
                _ = (channel, ts, text)
                return {}

            def open_dm(self, user_id: str) -> str:
                _ = user_id
                return ""

            def get_permalink(self, *, channel: str, ts: str) -> str:
                _ = (channel, ts)
                return ""

            def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
                _ = (channel, ts, emoji)
                return {}

            def resolve_user_id(self, handle: str) -> str:
                _ = handle
                return ""

        _own_loop("owner")
        backend = FakeBackend(
            dms=[
                {
                    "ts": "1700000000.0001",
                    "user": "U0A72P7CK0A",
                    "channel": "D0B36P8LU86",
                    "text": "ship it",
                }
            ]
        )
        SlackDmInboundScanner(backend=backend, overlay="demo").scan()
        assert PendingChatInjection.objects.filter(consumed_at__isnull=True).count() == 1

        handle_inject_pending_chat({"session_id": "owner"})

        out = capsys.readouterr().out
        assert "ship it" in out
        assert PendingChatInjection.objects.filter(consumed_at__isnull=True).count() == 0
