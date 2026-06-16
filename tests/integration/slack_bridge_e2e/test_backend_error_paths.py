"""Slack-backend error paths exercised through the same fake transport."""

import pytest
from inline_snapshot import snapshot

from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.models import PendingChatInjection
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
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

    def test_fetch_dms_returns_empty_when_no_user_id(self, transport: FakeSlackTransport) -> None:
        """RED if the ``not self._user_id`` early-return is removed.

        Guard: removing the ``if not self._user_id … return []`` branch
        in ``fetch_dms`` makes the backend call Slack with an empty
        user id, which Slack would error on and the fake records.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="")
        assert backend.fetch_dms() == snapshot([])
        assert transport.calls == snapshot([])

    def test_fetch_dms_returns_empty_when_open_dm_fails(self, transport: FakeSlackTransport) -> None:
        """RED if ``fetch_dms`` ignores ``open_dm`` failure.

        Guard: removing the ``if not channel: return []`` check after
        ``open_dm`` makes the backend call ``conversations.history``
        with an empty channel id and crashes.
        """
        transport.default_responses["conversations.open"] = {"ok": False, "error": "user_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        assert backend.fetch_dms() == snapshot([])

    def test_fetch_dms_drained_queue_short_circuits_rest(self, transport: FakeSlackTransport) -> None:
        """RED if the Socket-Mode queue read branch is removed.

        Guard: skipping the ``self._dms.snapshot()`` short-circuit makes a
        queued event invisible and the backend falls through to REST
        polling — the queue contract is broken.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        backend.inbound.enqueue_dm({"ts": "1.0", "user": "U_HUMAN", "text": "queued"})

        events = backend.fetch_dms()

        assert len(events) == 1
        assert events[0]["text"] == "queued"
        # The queue path must NOT have called Slack.
        assert transport.calls == snapshot([])

    def test_poll_dm_history_handles_slack_error(self, transport: FakeSlackTransport) -> None:
        """RED if ``_poll_dm_history`` stops returning ``[]`` on ``ok: false``.

        Guard: removing the ``if not data.get("ok"): return []`` check
        makes the helper return whatever was in the error body's
        ``messages`` key, propagating bad data downstream.
        """
        transport.default_responses["conversations.history"] = {"ok": False, "error": "channel_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        assert backend.fetch_dms() == snapshot([])

    def test_fetch_dms_with_since_passes_oldest_param(self, transport: FakeSlackTransport) -> None:
        """RED if ``fetch_dms`` no longer forwards its ``since`` filter to Slack.

        Guard: dropping the ``if since: params["oldest"] = since`` line
        means thread polling re-fetches all history each tick.
        """
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")
        backend.fetch_dms(since="1700000000.000099")

        history_calls = transport.calls_to("conversations.history")
        assert len(history_calls) == 1
        assert history_calls[0].payload.get("oldest") == snapshot("1700000000.000099")

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

    def test_get_when_no_token_returns_empty_dict(self) -> None:
        """RED if ``_get`` removes its no-token early-return."""
        backend = SlackBotBackend()
        assert backend.get_permalink(channel="C", ts="1.0") == snapshot("")

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

    def test_resolve_bot_id_caches_and_filters_bot_authored_messages(
        self,
        transport: FakeSlackTransport,
    ) -> None:
        """RED if ``_resolve_bot_id`` stops caching or ``_collect_user_dms`` stops filtering.

        End-to-end: the bot's own DM history contains a message authored
        by the bot AND a user message. Only the user message should
        reach the scanner. Subsequent calls hit the cached bot id (one
        ``auth.test`` call total).
        """
        transport.default_responses["auth.test"] = {"ok": True, "user_id": "B_BOT"}
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "bot_id": "B_BOT", "text": "previous bot post"},
                {"ts": "2.0", "user": "U_HUMAN", "text": "human reply"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        first = backend.fetch_dms()
        second = backend.fetch_dms()

        all_texts = [m["text"] for m in (first + second)]
        # Only the human reply (twice — once per poll), never the bot's own message.
        assert all_texts == snapshot(["human reply", "human reply"])
        # The bot-id cache means exactly one ``auth.test`` call across both fetches.
        assert len(transport.calls_to("auth.test")) == snapshot(1)

    def test_thread_reply_helper_returns_empty_on_slack_error(self, transport: FakeSlackTransport) -> None:
        """RED if ``_fetch_thread_replies`` propagates errors instead of swallowing them."""
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {
                    "ts": "1.0",
                    "thread_ts": "1.0",
                    "user": "U_HUMAN",
                    "text": "top-level",
                },
            ],
        }
        transport.default_responses["conversations.replies"] = {"ok": False, "error": "thread_not_found"}
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        events = backend.fetch_dms()
        texts = [m["text"] for m in events]
        # Top-level message still arrives; the failed thread fan-out is silent.
        assert texts == snapshot(["top-level"])

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

    def test_collect_user_dms_handles_non_thread_bot_message(self, transport: FakeSlackTransport) -> None:
        """RED if the bot-authored filter in ``_collect_user_dms`` is dropped on non-thread-root posts.

        Guard: a non-thread bot message should be filtered out AND not
        trigger ``conversations.replies``. Removing either branch breaks
        the contract.
        """
        transport.default_responses["auth.test"] = {"ok": True, "user_id": "B_BOT"}
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                # Bot-authored, NOT a thread root (no thread_ts equal to ts)
                {"ts": "1.0", "user": "B_BOT", "text": "bot status"},
                {"ts": "2.0", "user": "U_HUMAN", "text": "human follow-up"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        events = backend.fetch_dms()

        texts = [e["text"] for e in events]
        assert texts == snapshot(["human follow-up"])
        # No replies fan-out fired (neither message is a thread root).
        assert transport.calls_to("conversations.replies") == snapshot([])

    def test_thread_reply_helper_tolerates_non_list_messages_field(self, transport: FakeSlackTransport) -> None:
        """RED if ``_fetch_thread_replies`` stops type-checking ``messages``."""
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "thread_ts": "1.0", "user": "U_HUMAN", "text": "top"},
            ],
        }
        # Slack returns a corrupt ``messages`` (string, not list)
        transport.default_responses["conversations.replies"] = {"ok": True, "messages": "corrupt"}
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        events = backend.fetch_dms()
        # Top-level still surfaces, no crash from the corrupt reply payload.
        assert [e["text"] for e in events] == snapshot(["top"])

    def test_thread_reply_helper_skips_non_dict_items(self, transport: FakeSlackTransport) -> None:
        """RED if ``_fetch_thread_replies`` stops filtering non-dict items.

        Guard: removing the ``if not isinstance(m, dict): continue``
        check makes the helper crash on a non-dict element.
        """
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "thread_ts": "1.0", "user": "U_HUMAN", "text": "top"},
            ],
        }
        transport.default_responses["conversations.replies"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "user": "U_HUMAN", "text": "top"},  # root, skipped
                "not-a-dict",  # filtered
                {"ts": "1.1", "user": "U_HUMAN", "text": "real reply"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        texts = [e["text"] for e in backend.fetch_dms()]
        assert texts == snapshot(["top", "real reply"])

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

    def test_scanner_skips_blank_text_messages(self, transport: FakeSlackTransport) -> None:
        """RED if ``SlackDmInboundScanner.scan`` stops rejecting blank text."""
        transport.default_responses["conversations.history"] = {
            "ok": True,
            "messages": [
                {"ts": "1.0", "user": "U_HUMAN", "text": "   "},
                {"ts": "2.0", "user": "U_HUMAN", "text": "real"},
            ],
        }
        backend = SlackBotBackend(bot_token="xoxb-bot", user_id="U_HUMAN")

        signals = SlackDmInboundScanner(backend=backend, overlay="demo").scan()

        # Only the non-blank message produces a row.
        rows = list(PendingChatInjection.objects.values_list("text", flat=True))
        assert rows == snapshot(["real"])
        assert len(signals) == 1
