"""Unit tests for the bot-identity primitives (#1346 / #2089).

The backend-layer single owner of "is this Slack message the bot's own?"
logic, relocated from the loop scanner so the backend can apply it at its
read chokepoint. These cover :func:`resolve_own_identity`'s defensive
coercions of a malformed ``auth.test`` body.
"""

from dataclasses import dataclass, field

from teatree.backends.slack.self_identity import (
    OwnSlackIdentity,
    is_on_behalf_posted,
    is_self_authored,
    is_thread_root,
    resolve_own_identity,
)
from teatree.types import RawAPIDict


@dataclass
class _FakeBackend:
    auth_response: RawAPIDict = field(default_factory=dict)

    def auth_test(self) -> RawAPIDict:
        return self.auth_response


class TestResolveOwnIdentity:
    def test_resolves_user_and_bot_id(self) -> None:
        backend = _FakeBackend(auth_response={"ok": True, "user_id": "U1", "bot_id": "B1"})

        identity = resolve_own_identity(backend)

        assert identity == OwnSlackIdentity(user_id="U1", bot_id="B1")

    def test_not_ok_response_returns_none(self) -> None:
        backend = _FakeBackend(auth_response={"ok": False, "error": "invalid_auth"})

        assert resolve_own_identity(backend) is None

    def test_empty_response_returns_none(self) -> None:
        assert resolve_own_identity(_FakeBackend()) is None

    def test_non_str_ids_coerced_to_empty_and_unresolvable_returns_none(self) -> None:
        # A malformed body where both ids are non-strings coerces both to ""
        # → not resolvable → None (the line 84/86/90 branches).
        backend = _FakeBackend(auth_response={"ok": True, "user_id": 123, "bot_id": None})

        assert resolve_own_identity(backend) is None

    def test_non_str_user_id_keeps_valid_bot_id(self) -> None:
        backend = _FakeBackend(auth_response={"ok": True, "user_id": 123, "bot_id": "B1"})

        identity = resolve_own_identity(backend)

        assert identity == OwnSlackIdentity(user_id="", bot_id="B1")


class TestIsSelfAuthored:
    def test_matches_on_user_id(self) -> None:
        identity = OwnSlackIdentity(user_id="U1", bot_id="B1")
        assert is_self_authored({"user": "U1"}, identity) is True

    def test_matches_on_bot_id(self) -> None:
        identity = OwnSlackIdentity(user_id="U1", bot_id="B1")
        assert is_self_authored({"bot_id": "B1"}, identity) is True

    def test_user_authored_message_is_not_self(self) -> None:
        identity = OwnSlackIdentity(user_id="U1", bot_id="B1")
        assert is_self_authored({"user": "U_OTHER"}, identity) is False

    def test_bot_id_matches_against_user_id_when_bot_id_unknown(self) -> None:
        # auth.test returned only user_id; Slack stamped it into the post's bot_id.
        identity = OwnSlackIdentity(user_id="B_BOT", bot_id="")
        assert is_self_authored({"bot_id": "B_BOT"}, identity) is True

    def test_empty_identity_never_matches(self) -> None:
        assert is_self_authored({"user": "U1", "bot_id": "B1"}, OwnSlackIdentity(user_id="", bot_id="")) is False


class TestIsThreadRoot:
    def test_thread_root_when_thread_ts_equals_ts(self) -> None:
        assert is_thread_root({"ts": "1.0", "thread_ts": "1.0"}) is True

    def test_not_root_when_thread_ts_differs(self) -> None:
        assert is_thread_root({"ts": "2.0", "thread_ts": "1.0"}) is False

    def test_not_root_without_thread_ts(self) -> None:
        assert is_thread_root({"ts": "1.0"}) is False


class TestIsOnBehalfPosted:
    """#1941: distinguish an on-behalf app post from a human-typed DM."""

    def test_true_when_api_app_id_present(self) -> None:
        assert is_on_behalf_posted({"user": "U1", "api_app_id": "A0DEMOAPP1"}) is True

    def test_false_when_api_app_id_absent(self) -> None:
        assert is_on_behalf_posted({"user": "U1", "text": "hi"}) is False

    def test_false_when_api_app_id_empty_string(self) -> None:
        assert is_on_behalf_posted({"user": "U1", "api_app_id": ""}) is False

    def test_false_when_api_app_id_non_string(self) -> None:
        assert is_on_behalf_posted({"user": "U1", "api_app_id": 12345}) is False
