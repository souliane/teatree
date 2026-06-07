"""Thread-root read-back helpers for the Slack-answer pipeline (#2061).

A user message that is itself a thread reply has a non-root ``ts``. Posting
with ``--thread-ts <that ts>`` re-parents to the thread ROOT, so any read-back
keyed on the user-message ts (rather than the root) misses the just-posted
reply: the dedup check sees "no prior reply" and posts a duplicate, and the
delivery verification declares a delivered reply absent.

These helpers resolve the root FIRST, then read the root's replies — the
single chokepoint both the dedup and the verification use.
"""

from dataclasses import dataclass, field

import pytest

from teatree.loop.slack_answer.thread_readback import bot_reply_present_in_thread, resolve_thread_root
from teatree.types import RawAPIDict

BOT_UID = "UBOT"
USER_UID = "UUSER"


@dataclass
class FakeBackend:
    """MessagingBackend stub: a per-(channel) message store keyed by ts."""

    messages: dict[str, RawAPIDict] = field(default_factory=dict)
    replies_by_root: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    auth: RawAPIDict = field(default_factory=lambda: {"ok": True, "user_id": BOT_UID})
    fetch_message_raises: bool = False
    fetch_replies_raises: bool = False

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = channel
        if self.fetch_message_raises:
            msg = "history 503"
            raise RuntimeError(msg)
        return self.messages.get(ts, {})

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = channel
        if self.fetch_replies_raises:
            msg = "replies 503"
            raise RuntimeError(msg)
        return list(self.replies_by_root.get(thread_ts, []))

    def auth_test(self) -> RawAPIDict:
        return self.auth


class TestResolveThreadRoot:
    def test_non_root_user_message_resolves_to_its_thread_root(self) -> None:
        backend = FakeBackend(messages={"reply.ts": {"ts": "reply.ts", "thread_ts": "root.ts", "user": USER_UID}})

        root = resolve_thread_root(backend, channel="D1", ts="reply.ts")

        assert root == "root.ts"

    def test_root_message_resolves_to_itself(self) -> None:
        backend = FakeBackend(messages={"root.ts": {"ts": "root.ts", "thread_ts": "root.ts", "user": USER_UID}})

        assert resolve_thread_root(backend, channel="D1", ts="root.ts") == "root.ts"

    def test_standalone_message_with_no_thread_ts_resolves_to_itself(self) -> None:
        backend = FakeBackend(messages={"solo.ts": {"ts": "solo.ts", "user": USER_UID}})

        assert resolve_thread_root(backend, channel="D1", ts="solo.ts") == "solo.ts"

    def test_unfetchable_message_falls_back_to_the_given_ts(self) -> None:
        backend = FakeBackend(messages={})

        assert resolve_thread_root(backend, channel="D1", ts="x.ts") == "x.ts"

    def test_fetch_raise_falls_back_to_the_given_ts(self) -> None:
        backend = FakeBackend(fetch_message_raises=True)

        assert resolve_thread_root(backend, channel="D1", ts="x.ts") == "x.ts"


class TestBotReplyPresentInThread:
    def test_finds_a_bot_reply_under_the_thread_root(self) -> None:
        backend = FakeBackend(
            replies_by_root={
                "root.ts": [
                    {"ts": "root.ts", "user": USER_UID, "text": "question"},
                    {"ts": "reply.ts", "user": BOT_UID, "text": "the answer"},
                ]
            }
        )

        assert bot_reply_present_in_thread(backend, channel="D1", thread_root="root.ts")

    def test_user_only_thread_has_no_bot_reply(self) -> None:
        backend = FakeBackend(replies_by_root={"root.ts": [{"ts": "root.ts", "user": USER_UID, "text": "q"}]})

        assert not bot_reply_present_in_thread(backend, channel="D1", thread_root="root.ts")

    def test_empty_thread_has_no_bot_reply(self) -> None:
        backend = FakeBackend()

        assert not bot_reply_present_in_thread(backend, channel="D1", thread_root="root.ts")

    def test_read_raise_reports_absent_conservatively(self) -> None:
        backend = FakeBackend(fetch_replies_raises=True)

        assert not bot_reply_present_in_thread(backend, channel="D1", thread_root="root.ts")

    def test_unresolved_bot_identity_reports_absent_conservatively(self) -> None:
        backend = FakeBackend(
            auth={"ok": False},
            replies_by_root={"root.ts": [{"ts": "reply.ts", "user": BOT_UID, "text": "a"}]},
        )

        assert not bot_reply_present_in_thread(backend, channel="D1", thread_root="root.ts")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
