"""Inbound bridge: REST polling → row → drain → additionalContext."""

from pathlib import Path

import pytest
from inline_snapshot import snapshot

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport, _own_loop

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]


class TestInboundBridgeEndToEnd:
    """Slack DM → REST poll → row → ``UserPromptSubmit`` drain → stdout.

    Each test runs the real ``SlackBotBackend.fetch_dms`` against the
    fake transport, then the real scanner, then the real hook handler.
    """

    def test_dm_lands_as_pending_chat_injection_row(self, transport: FakeSlackTransport) -> None:
        """RED if ``SlackBotBackend.fetch_dms`` REST poll branch is removed.

        Guard: deleting the ``conversations.history`` poll fallback in
        ``fetch_dms`` (the branch that runs when the Socket-Mode queue
        snapshot is empty) turns this RED — no row would land.
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1700000000.0001", "user": "U_HUMAN", "text": "ship PR 42"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        row = PendingChatInjection.objects.get()
        assert (row.overlay, row.slack_ts, row.text, row.user_id, row.channel) == snapshot(
            ("demo", "1700000000.0001", "ship PR 42", "U_HUMAN", "D-USER")
        )
        assert [s.kind for s in signals] == snapshot(["slack.user_reply"])

    def test_thread_reply_lands_as_pending_chat_injection_row(self, transport: FakeSlackTransport) -> None:
        """RED if the ``conversations.replies`` fan-out (#1046) is reverted.

        Guard: deleting the ``_fetch_thread_replies`` invocation in
        ``_collect_user_dms`` makes the thread reply invisible to the
        scanner — only the top-level message would persist.
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {
                    "ts": "1700000000.0001",
                    "thread_ts": "1700000000.0001",
                    "user": "U_HUMAN",
                    "text": "top-level question",
                },
            ],
        }
        transport.default_responses["conversations.replies"] = {
            "ok": True,
            "messages": [
                {"ts": "1700000000.0001", "user": "U_HUMAN", "text": "top-level question"},
                {"ts": "1700000000.0002", "user": "U_HUMAN", "text": "follow-up in thread"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        texts = list(PendingChatInjection.objects.order_by("slack_ts").values_list("text", flat=True))
        assert texts == snapshot(["top-level question", "follow-up in thread"])

    def test_channel_stamp_present_on_rest_polled_event(self, transport: FakeSlackTransport) -> None:
        """RED if the ``msg.setdefault("channel", channel)`` stamp (#1043) is reverted.

        Guard: removing the ``setdefault`` line in ``_collect_user_dms``
        means the scanner sees ``channel=""`` and
        ``PendingChatInjection.record`` rejects the row (its guard
        requires ``channel`` to be truthy).
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1700000000.0001", "user": "U_HUMAN", "text": "needs channel"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        assert PendingChatInjection.objects.get().channel == snapshot("D-USER")

    def test_userpromptsubmit_drain_injects_additional_context(
        self,
        transport: FakeSlackTransport,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """RED if the drain handler stops printing the additionalContext block.

        Guard: removing the ``print(...)`` line in
        ``handle_inject_pending_chat`` (or removing the ``row.consume()``
        call) breaks one of the two asserts. The stdout shape is
        captured via inline-snapshot — a refactor that changes the
        emitted line format will surface as a snapshot diff.
        """
        from hooks.scripts.hook_router import handle_inject_pending_chat  # noqa: PLC0415

        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [{"ts": "1700000000.0001", "user": "U_HUMAN", "text": "drain me"}],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        SlackDmInboundScanner(backend=backend, overlay="").scan()
        _own_loop("owner", monkeypatch, tmp_path)

        handle_inject_pending_chat({"session_id": "owner"})

        out = capsys.readouterr().out
        assert out == snapshot("""\
You have 1 new Slack DM reply(ies) from the user:
User replied on Slack at 1700000000.0001: drain me
""")
        assert PendingChatInjection.objects.get().consumed_at is not None

    def test_consumed_row_not_redrained_on_second_session(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """RED if ``PendingChatInjection.consume`` stops gating on ``consumed_at``.

        Guard: removing the ``consumed_at__isnull=True`` filter in
        ``consume()`` lets the second drain re-emit the message, which
        re-injects an already-handled DM into the agent.
        """
        from hooks.scripts.hook_router import handle_inject_pending_chat  # noqa: PLC0415

        PendingChatInjection.record(channel="D-USER", slack_ts="1.0", text="once-only", overlay="")
        _own_loop("session-A", monkeypatch, tmp_path)
        handle_inject_pending_chat({"session_id": "session-A"})
        capsys.readouterr()  # drain stdout

        _own_loop("session-B", monkeypatch, tmp_path)
        handle_inject_pending_chat({"session_id": "session-B"})

        assert capsys.readouterr().out == snapshot("")

    def test_scanner_overpoll_does_not_emit_duplicate_signals(self, transport: FakeSlackTransport) -> None:
        """RED if ``SlackDmInboundScanner`` re-emits signals for already-recorded ``ts``.

        Guard: removing the ``if row is None: continue`` branch in
        ``SlackDmInboundScanner.scan`` makes a second poll emit a
        duplicate signal even though the row already exists.
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [{"ts": "1700000000.0001", "user": "U_HUMAN", "text": "ping"}],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        scanner = SlackDmInboundScanner(backend=backend, overlay="demo")

        first = scanner.scan()
        second = scanner.scan()

        assert ([s.kind for s in first], [s.kind for s in second]) == snapshot((["slack.user_reply"], []))
        assert PendingChatInjection.objects.count() == 1

    def test_double_unique_constraint_per_overlay_slack_ts(self) -> None:
        """RED if the ``uniq_pendingchat_overlay_ts`` constraint is dropped.

        Guard: removing the ``UniqueConstraint`` from
        ``PendingChatInjection.Meta.constraints`` permits duplicates and
        the test expecting an ``IntegrityError`` flips to passing the
        insert.
        """
        from django.db import IntegrityError, transaction  # noqa: PLC0415

        PendingChatInjection.objects.create(overlay="demo", channel="D-USER", slack_ts="dup", text="first")
        with pytest.raises(IntegrityError), transaction.atomic():
            PendingChatInjection.objects.create(overlay="demo", channel="D-USER", slack_ts="dup", text="second")
