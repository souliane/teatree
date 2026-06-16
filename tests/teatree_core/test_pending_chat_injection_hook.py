"""Tests for ``handle_inject_pending_chat`` UserPromptSubmit hook (#1014 followup).

The drain side of the Slack inbound bridge — WS2 / WS4. The handler
reads unconsumed :class:`PendingChatInjection` rows targeted at the
user and writes them into the next user turn's ``additionalContext``
so a Slack DM reaches the agent as if the user had typed it.

**Drain eligibility (corrected):** ANY interactive Claude Code session
may drain the queue — the original ``_session_owns_loop`` gate was the
wrong invariant. The autonomous ``t3 loop start`` session owns the
loop record but never receives ``UserPromptSubmit`` events, so the
owner-gate caused the queue to pile up (32 unconsumed rows observed
in production). At-most-once delivery is preserved by the durable
single-use ``consume()`` transition plus the ``(overlay, slack_ts)``
``UniqueConstraint`` — the owner-gate added nothing real.
"""

import os
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _OWNER_LOOP, _write_loop_registry, handle_inject_pending_chat
from teatree.core.models import PendingChatInjection

# ast-grep-ignore: ac-django-no-pytest-django-db
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


class TestDrain:
    def test_owner_session_drains_one_pending_row(self, capsys: pytest.CaptureFixture[str]) -> None:
        _own_loop("owner-1")
        PendingChatInjection.record(
            channel="D0DEMOTEAM1",
            slack_ts="1700000000.0001",
            text="Please review PR #42",
            user_id="U0DEMOUSER1",
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


class TestAnySessionDrains:
    """Drain eligibility (#1014 followup).

    The original implementation gated the drain on ``_session_owns_loop``,
    matching the §5.6 self-pump owner discipline. That was the wrong
    invariant for the inbound bridge: the loop-owner record points at the
    autonomous ``t3 loop start`` session, which never receives
    ``UserPromptSubmit`` events. User DMs land in the interactive Claude
    Code session — a non-owner session — so the queue piled up unconsumed
    (32 rows observed in production). The fix drops the owner gate; these
    tests are the regression guard.

    At-most-once delivery is preserved by the durable single-use
    ``consume()`` transition (a second caller sees ``consumed_at`` set)
    and the ``(overlay, slack_ts)`` ``UniqueConstraint`` (deduping the
    ingest side), so the owner-gate was redundant safety, not a real
    correctness primitive.
    """

    def test_non_owner_session_drains(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Regression: a session that does NOT own the loop must still drain.

        Anti-vacuous proof: reverting the fix (re-introducing the
        ``_session_owns_loop`` gate at the head of the handler) turns
        this RED — the message stays in ``out`` empty and the row's
        ``consumed_at`` stays ``None``.
        """
        _own_loop("the-loop-owner")
        PendingChatInjection.record(
            channel="D0DEMOTEAM1",
            slack_ts="1700000000.0001",
            text="merge PR #42",
            user_id="U0DEMOUSER1",
        )

        handle_inject_pending_chat({"session_id": "interactive-session-not-owner"})

        out = capsys.readouterr().out
        assert "merge PR #42" in out
        assert "Slack" in out
        row = PendingChatInjection.objects.get()
        assert row.consumed_at is not None
        assert row.is_pending is False

    def test_non_owner_drains_multiple_rows(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Regression: the full backlog drains for a non-owner session."""
        _own_loop("loop-owner-elsewhere")
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="reply one")
        PendingChatInjection.record(channel="D", slack_ts="2.0", text="reply two")

        handle_inject_pending_chat({"session_id": "interactive"})

        out = capsys.readouterr().out
        assert "reply one" in out
        assert "reply two" in out
        assert PendingChatInjection.objects.filter(consumed_at__isnull=True).count() == 0

    def test_drain_works_when_no_owner_registered(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No loop-owner record exists ⇒ the queue still drains.

        Models a fresh interactive session before ``t3 loop start`` has
        claimed the registry. The user's queued reply must still surface.
        """
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="ping the agent")

        handle_inject_pending_chat({"session_id": "any-interactive"})

        out = capsys.readouterr().out
        assert "ping the agent" in out
        assert PendingChatInjection.objects.get().is_pending is False

    def test_missing_session_id_is_still_a_no_op(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An empty ``session_id`` is a malformed payload — no drain."""
        _own_loop("owner")
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="msg")

        handle_inject_pending_chat({})

        assert capsys.readouterr().out == ""
        assert PendingChatInjection.objects.get().is_pending is True

    def test_concurrent_drain_at_most_once_via_consume(self, capsys: pytest.CaptureFixture[str]) -> None:
        """At-most-once delivery: ``consume()`` enforces single-emit across sessions.

        Two interactive sessions both invoke the drain on the same row.
        The durable ``consume()`` single-use transition guarantees only
        one of them emits the text; the second sees ``consumed_at`` set
        and is a clean no-op. The owner-gate was redundant safety on top
        of this primitive.
        """
        PendingChatInjection.record(channel="D", slack_ts="1.0", text="exactly once")

        handle_inject_pending_chat({"session_id": "session-A"})
        first = capsys.readouterr().out

        handle_inject_pending_chat({"session_id": "session-B"})
        second = capsys.readouterr().out

        assert "exactly once" in first
        assert second == ""
        assert PendingChatInjection.objects.filter(consumed_at__isnull=True).count() == 0


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
            user_id="U0DEMOUSER1",
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

            def auth_test(self) -> RawAPIDict:
                # #1346 self-filter probe — return a resolved identity so
                # genuine user messages are not dropped by the fail-closed
                # path for this round-trip integration test.
                return {"ok": True, "user_id": "U_BOT_SELF", "bot_id": "B_BOT_SELF"}

        _own_loop("owner")
        backend = FakeBackend(
            dms=[
                {
                    "ts": "1700000000.0001",
                    "user": "U0DEMOUSER1",
                    "channel": "D0DEMOTEAM1",
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
