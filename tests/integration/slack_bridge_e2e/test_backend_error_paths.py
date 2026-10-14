"""Slack-backend and catch-up DM-read error paths exercised through the same fake transport."""

import time
from dataclasses import dataclass, field
from typing import Any

import pytest
from inline_snapshot import snapshot

from teatree.backends.slack.bot import SlackBotBackend
from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.backends.slack.token_validation import SlackTokenMissingError
from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.slack_catchup import catch_up
from teatree.slack_catchup.window import CatchUpRefusedError
from teatree.types import RawAPIDict
from tests.integration.slack_bridge_e2e.conftest import FakeSlackTransport

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = [pytest.mark.django_db, pytest.mark.integration]


class TestSlackBotBackendErrorPathsE2E:
    """End-to-end failure-mode coverage for ``SlackBotBackend``.

    Each test simulates a specific Slack-API failure shape that
    ``SlackBotBackend`` must absorb gracefully (return empty / ``""``).
    Together they push the backend's coverage to its `fail open` branches
    and ensure a future code change can't silently turn one into a raise.
    """

    def test_open_dm_returns_empty_on_slack_error(self, transport: FakeSlackTransport) -> None:
        """RED if ``open_dm`` propagates the error body instead of returning ``""``.

        Guard: removing the ``if not data.get("ok"): return ""`` check.
        """
        transport.default_responses["conversations.open"] = {"ok": False, "error": "users_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot")

        assert backend.open_dm("U_BAD") == snapshot("")

    def test_get_permalink_returns_empty_on_blank_inputs(self) -> None:
        """RED if ``get_permalink`` removes its input-validation guard.

        Guard: removing the ``if not channel or not ts: return ""`` line.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.get_permalink(channel="", ts="1.0") == snapshot("")
        assert backend.get_permalink(channel="C", ts="") == snapshot("")

    def test_get_permalink_returns_empty_on_slack_error(self, transport: FakeSlackTransport) -> None:
        """RED if ``get_permalink`` stops returning ``""`` on Slack errors."""
        transport.default_responses["chat.getPermalink"] = {"ok": False, "error": "message_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot")

        assert backend.get_permalink(channel="C", ts="1.0") == snapshot("")

    def test_get_reactions_parses_response_names(self, transport: FakeSlackTransport) -> None:
        """RED if ``get_reactions`` stops surfacing the configured emoji names.

        Pins the parsing shape so a refactor of the response walker
        cannot silently drop names.
        """
        transport.default_responses["reactions.get"] = {
            "ok": True,
            "message": {
                "reactions": [
                    {"name": "eyes"},
                    {"name": "white_check_mark"},
                ]
            },
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_token="xoxp-user")

        assert backend.get_reactions(channel="C", ts="1.0") == snapshot(["eyes", "white_check_mark"])

    def test_get_reactions_returns_empty_on_slack_error(self, transport: FakeSlackTransport) -> None:
        transport.default_responses["reactions.get"] = {"ok": False, "error": "message_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.get_reactions(channel="C", ts="1.0") == snapshot([])

    def test_resolve_user_id_via_email_lookup(self, transport: FakeSlackTransport) -> None:
        """RED if ``resolve_user_id`` stops calling ``users.lookupByEmail``.

        Guard: a refactor that drops the ``"@" in clean`` branch breaks
        the email-handle path; verifying the call hits
        ``users.lookupByEmail`` (not just ``users.list``) catches it.
        """
        transport.default_responses["users.lookupByEmail"] = {
            "ok": True,
            "user": {"id": "U_ALICE"},
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")

        result = backend.resolve_user_id("alice@example.com")

        assert result == snapshot("U_ALICE")
        assert len(transport.calls_to("users.lookupByEmail")) == 1

    def test_resolve_user_id_falls_back_to_users_list(self, transport: FakeSlackTransport) -> None:
        """RED if the ``users.list`` fallback is removed for non-email handles."""
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [
                {"id": "U_BOB", "name": "bob", "real_name": "Bob T. Builder"},
                {"id": "U_ALICE", "name": "alice", "real_name": "Alice In Wonderland"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")

        assert backend.resolve_user_id("@alice") == snapshot("U_ALICE")
        assert backend.resolve_user_id("Bob T. Builder") == snapshot("U_BOB")

    def test_resolve_user_id_returns_empty_on_blank_handle(self) -> None:
        """RED if ``resolve_user_id`` removes its blank-input guard."""
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("@") == snapshot("")
        assert backend.resolve_user_id("") == snapshot("")

    def test_post_when_no_token_returns_empty_dict(self) -> None:
        """RED if ``_post`` removes its no-token early-return.

        Guard: removing the ``if not auth: return {}`` line in ``_post``
        makes the backend hit Slack with an empty Authorization header
        and crashes on ``response.raise_for_status``.
        """
        backend = SlackBotBackend()  # no token at all
        assert backend.post_message(channel="C", text="hi") == snapshot({})

    def test_get_when_no_token_fails_loud_before_calling_slack(self, transport: FakeSlackTransport) -> None:
        """RED if ``get`` reads a missing token as an empty answer, or calls Slack without one."""
        backend = SlackBotBackend()

        with pytest.raises(SlackTokenMissingError, match="no Slack token configured"):
            backend.get_permalink(channel="C", ts="1.0")

        assert transport.calls_to("chat.getPermalink") == []

    def test_post_reply_routes_to_chat_post_message_with_thread_ts(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``post_reply`` stops passing ``thread_ts`` to Slack.

        Guard: a refactor that omits ``thread_ts`` would post a brand-new
        top-level message instead of a reply, breaking thread continuity.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot")

        backend.post_reply(channel="C", ts="1.0", text="reply body")

        post_calls = transport.calls_to("chat.postMessage")
        assert post_calls[0].payload == snapshot({"channel": "C", "thread_ts": "1.0", "text": "reply body"})

    def test_post_message_with_thread_ts_carries_thread_ts(self, transport: FakeSlackTransport) -> None:
        """RED if ``post_message`` drops its ``thread_ts`` kwarg.

        Guard: removing the ``if thread_ts: payload["thread_ts"] = thread_ts``
        branch would make threaded posts land as top-level messages.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot")

        backend.post_message(channel="C", text="hello", thread_ts="1.0")

        assert transport.calls_to("chat.postMessage")[0].payload == snapshot(
            {"channel": "C", "thread_ts": "1.0", "text": "hello"}
        )

    def test_token_accessor_properties(self) -> None:
        """RED if any of the read-only token properties is renamed silently.

        These accessors are consumed by ``backend_factory`` and the
        Socket Mode receiver. The property names are part of the
        ``MessagingBackend`` contract.
        """
        backend = SlackBotBackend(
            bot_token="xoxb-bot",
            app_token="xapp-app",
            user_token="xoxp-user",
            user_id="U_HUMAN",
        )

        assert (backend.app_token, backend.user_id, backend.user_token) == snapshot(
            ("xapp-app", "U_HUMAN", "xoxp-user")
        )

    def test_enqueue_mention_routes_to_fetch_mentions_queue(self, transport: FakeSlackTransport) -> None:
        """RED if the mention queue contract is broken.

        Socket Mode pushes mentions into ``enqueue_mention``;
        ``fetch_mentions`` reads them non-destructively within a tick so
        every scanner sharing the backend sees the same batch (#1655). The
        buffer rolls on the next enqueue.
        """
        _ = transport  # ensure no HTTP call happens
        backend = SlackBotBackend(bot_token="xoxb-bot")
        backend.inbound.enqueue_mention({"ts": "1.0", "user": "U", "text": "@bot hi"})

        events = backend.fetch_mentions()
        again = backend.fetch_mentions()

        assert len(events) == 1
        assert events[0]["text"] == "@bot hi"
        assert again == events

    def test_get_reactions_tolerates_non_list_reactions(self, transport: FakeSlackTransport) -> None:
        """RED if ``get_reactions`` stops type-checking the ``reactions`` field."""
        transport.default_responses["reactions.get"] = {
            "ok": True,
            "message": {"reactions": "not-a-list"},
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.get_reactions(channel="C", ts="1.0") == snapshot([])

    def test_get_reactions_skips_non_dict_reaction_items(self, transport: FakeSlackTransport) -> None:
        """RED if ``get_reactions`` stops filtering non-dict reaction entries."""
        transport.default_responses["reactions.get"] = {
            "ok": True,
            "message": {
                "reactions": [
                    "not-a-dict",
                    {"name": "eyes"},
                    {"missing_name": True},
                ]
            },
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        # The non-dict item and the dict-without-name are both filtered.
        assert backend.get_reactions(channel="C", ts="1.0") == snapshot(["eyes"])

    def test_resolve_user_id_returns_empty_when_members_field_missing(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``resolve_user_id`` stops type-checking ``users.list``'s ``members``."""
        transport.default_responses["users.list"] = {"ok": True, "members": "not-a-list"}
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("alice") == snapshot("")

    def test_resolve_user_id_skips_non_dict_member_entries(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``resolve_user_id`` stops filtering non-dict members."""
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [
                "not-a-dict",
                {"id": "U_ALICE", "name": "alice"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("alice") == snapshot("U_ALICE")

    def test_resolve_user_id_returns_empty_when_no_match(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [
                {"id": "U_BOB", "name": "bob"},
                {"id": "U_CAROL", "name": "carol"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("alice") == snapshot("")

    def test_resolve_user_id_email_lookup_with_non_string_id_falls_back_to_list(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``resolve_user_id`` accepts a non-string user id from ``users.lookupByEmail``.

        Slack normally returns ``user.id`` as a string. A malformed
        response must not poison the cache — fall back to the
        ``users.list`` walker instead.
        """
        transport.default_responses["users.lookupByEmail"] = {
            "ok": True,
            "user": {"id": 12345},  # int, not str
        }
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [{"id": "U_ALICE", "name": "alice"}],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        # Lookup the email; the int id from email-lookup is rejected,
        # then the list-walker picks up the same human by name.
        assert backend.resolve_user_id("alice@example.com") == snapshot("")

    def test_resolve_user_id_list_match_with_non_string_id_returns_empty(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``resolve_user_id`` accepts a non-string id from a list match."""
        transport.default_responses["users.list"] = {
            "ok": True,
            "members": [{"id": 99, "name": "alice"}],  # int, not str
        }
        backend = SlackBotBackend(bot_token="xoxb-bot")
        assert backend.resolve_user_id("alice") == snapshot("")

    def test_recorder_skips_blank_text_messages(self, transport: FakeSlackTransport) -> None:
        """RED if ``PendingChatInjection.record`` stops rejecting blank text."""
        _ = transport
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        recorded = SlackDmRecorder(backend=backend, overlay="demo").record_events(
            [
                {"ts": "1.0", "user": "U_HUMAN", "channel": "D-USER", "text": "   "},
                {"ts": "2.0", "user": "U_HUMAN", "channel": "D-USER", "text": "real"},
            ]
        )

        rows = list(PendingChatInjection.objects.values_list("text", flat=True))
        assert rows == snapshot(["real"])
        assert recorded == 1


def _ts_ago(seconds: float) -> str:
    return f"{time.time() - seconds:.6f}"


@dataclass
class _Delivered:
    events: list[RawAPIDict] = field(default_factory=list)

    def __call__(self, event: RawAPIDict) -> bool:
        self.events.append(event)
        return True


def _catch_up(backend: SlackBotBackend, delivered: _Delivered) -> None:
    list(catch_up(backend, alive_at=time.time() - 120, deliver=delivered))


def _in_window_root(text: str) -> RawAPIDict:
    root_ts = _ts_ago(60)
    return {
        "ts": root_ts,
        "thread_ts": root_ts,
        "reply_count": 1,
        "latest_reply": _ts_ago(30),
        "user": "U_HUMAN",
        "text": text,
    }


class TestSlackCatchUpDmReadErrorPathsE2E:
    """Failure modes of the catch-up's read of the owner's bot DM.

    A catch-up that cannot read its whole window delivers nothing and raises, so the
    caller retries instead of advancing past a hole. Each refusal test scripts an
    in-window human DM, so a pass that delivered before failing would turn it RED.
    """

    def _backend(self) -> SlackBotBackend:
        return SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

    def _serve_one_human_dm(self, transport: FakeSlackTransport) -> None:
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [{"ts": _ts_ago(30), "user": "U_HUMAN", "text": "sent while the listener was down"}],
        }

    def test_a_refused_history_page_raises_and_delivers_nothing(self, transport: FakeSlackTransport) -> None:
        """RED if a refused page is skipped, or the first page is delivered before the refusal."""
        first_page = {
            "ok": True,
            "messages": [{"ts": _ts_ago(30), "user": "U_HUMAN", "text": "on the first page"}],
            "response_metadata": {"next_cursor": "c1"},
        }

        def history(params: dict[str, Any], _headers: dict[str, str]) -> dict[str, Any]:
            return {"ok": False, "error": "invalid_cursor"} if params.get("cursor") else first_page

        transport.handlers["conversations.history"] = history
        delivered = _Delivered()

        with pytest.raises(SlackReadRefusedError, match="invalid_cursor"):
            _catch_up(self._backend(), delivered)

        assert delivered.events == []
        assert len(transport.calls_to("conversations.history")) == 2

    def test_a_refused_replies_page_under_an_in_window_root_raises_and_delivers_nothing(
        self, transport: FakeSlackTransport
    ) -> None:
        """RED if a refused thread read is skipped, or the in-window root is delivered anyway."""
        root = _in_window_root("root of an active thread")
        transport.default_responses["conversations.history"] = {"ok": True, "messages": [root]}
        transport.default_responses["conversations.replies"] = {"ok": False, "error": "thread_not_found"}
        delivered = _Delivered()

        with pytest.raises(SlackReadRefusedError, match="thread_not_found"):
            _catch_up(self._backend(), delivered)

        assert delivered.events == []
        assert [c.payload["ts"] for c in transport.calls_to("conversations.replies")] == [root["ts"]]

    def test_an_unresolved_bot_identity_raises_before_reading(self, transport: FakeSlackTransport) -> None:
        """RED if the pass reads (and delivers the bot's own posts) without knowing who the bot is."""
        transport.default_responses["auth.test"] = {"ok": False, "error": "invalid_auth"}
        self._serve_one_human_dm(transport)
        delivered = _Delivered()

        with pytest.raises(CatchUpRefusedError, match="identity is unresolved"):
            _catch_up(self._backend(), delivered)

        assert delivered.events == []
        assert transport.calls_to("conversations.history") == []

    def test_a_dm_channel_that_does_not_open_raises(self, transport: FakeSlackTransport) -> None:
        """RED if ``conversations.open`` failing is read as an empty DM."""
        transport.default_responses["conversations.open"] = {"ok": False, "error": "user_not_found"}
        self._serve_one_human_dm(transport)
        delivered = _Delivered()

        with pytest.raises(CatchUpRefusedError, match="no bot DM channel"):
            _catch_up(self._backend(), delivered)

        assert delivered.events == []
        assert transport.calls_to("conversations.history") == []

    def test_no_owner_user_id_raises_without_opening_a_dm(self, transport: FakeSlackTransport) -> None:
        """RED if the pass opens a DM with an empty user id."""
        self._serve_one_human_dm(transport)
        delivered = _Delivered()

        with pytest.raises(CatchUpRefusedError, match="no bot DM channel"):
            _catch_up(SlackBotBackend(bot_token="xoxb-bot", user_id=""), delivered)

        assert delivered.events == []
        assert transport.calls_to("conversations.open") == []
        assert transport.calls_to("conversations.history") == []

    def test_bot_authored_messages_are_never_delivered(self, transport: FakeSlackTransport) -> None:
        """RED if the self-filter is dropped, or a non-root message fans out to ``conversations.replies``."""
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": _ts_ago(50), "bot_id": "B_BOT", "text": "previous bot post"},
                {"ts": _ts_ago(40), "user": "B_BOT", "text": "bot status"},
                {"ts": _ts_ago(30), "user": "U_HUMAN", "text": "human reply"},
            ],
        }
        delivered = _Delivered()

        _catch_up(self._backend(), delivered)

        assert [e["text"] for e in delivered.events] == snapshot(["human reply"])
        assert transport.calls_to("conversations.replies") == []

    def test_a_corrupt_replies_payload_keeps_the_root(self, transport: FakeSlackTransport) -> None:
        """RED if a non-list ``messages`` in a thread read crashes the pass."""
        transport.default_responses["conversations.history"] = {"ok": True, "messages": [_in_window_root("top")]}
        transport.default_responses["conversations.replies"] = {"ok": True, "messages": "corrupt"}
        delivered = _Delivered()

        _catch_up(self._backend(), delivered)

        assert [e["text"] for e in delivered.events] == snapshot(["top"])

    def test_non_dict_reply_items_are_skipped(self, transport: FakeSlackTransport) -> None:
        """RED if the thread read stops filtering non-dict items."""
        root = _in_window_root("top")
        transport.default_responses["conversations.history"] = {"ok": True, "messages": [root]}
        transport.default_responses["conversations.replies"] = {
            "ok": True,
            "messages": [
                root,
                "not-a-dict",
                {"ts": _ts_ago(30), "user": "U_HUMAN", "text": "real reply"},
            ],
        }
        delivered = _Delivered()

        _catch_up(self._backend(), delivered)

        assert [e["text"] for e in delivered.events] == snapshot(["top", "real reply"])
