"""Branch coverage for Slack web reads and shared retry decisions."""

from collections.abc import Callable

import pytest

from teatree.backends.slack.bot_errors import SlackReadRefusedError
from teatree.backends.slack.dm_history import read_thread_replies
from teatree.backends.slack.web_reads import (
    _MAX_USER_PAGES,
    Getter,
    read_channel_history_or_refuse,
    resolve_unique_active_user_id,
    resolve_user_id,
    resolve_usergroup_id,
)
from teatree.types import ChannelReadRefusedError, RawAPIDict


def _members_page(members: list[RawAPIDict], next_cursor: str = "") -> RawAPIDict:
    data: RawAPIDict = {"ok": True, "members": members}
    if next_cursor:
        data["response_metadata"] = {"next_cursor": next_cursor}
    return data


def _active_user(user_id: str, name: str) -> RawAPIDict:
    return {
        "id": user_id,
        "name": name,
        "deleted": False,
        "is_bot": False,
        "is_restricted": False,
        "is_ultra_restricted": False,
    }


class TestResolveUniqueActiveUserId:
    def test_user_id_resolves_only_for_active_non_guest_member(self) -> None:
        members = [_active_user("UACTIVE", "peer"), {**_active_user("UGUEST", "guest"), "is_restricted": True}]

        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _members_page(members)

        assert resolve_unique_active_user_id(get=get, handle="UACTIVE") == "UACTIVE"
        assert resolve_unique_active_user_id(get=get, handle="UGUEST") == ""

    def test_display_name_matches_casefolded_handle(self) -> None:
        member = {**_active_user("UPEER", "other"), "profile": {"display_name": "Review Peer"}}

        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _members_page([member])

        assert resolve_unique_active_user_id(get=get, handle="@review PEER") == "UPEER"

    def test_app_user_is_not_an_active_reviewer(self) -> None:
        member = {**_active_user("UAPP", "peer"), "is_app_user": True}

        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _members_page([member])

        assert resolve_unique_active_user_id(get=get, handle="peer") == ""

    def test_unique_active_match_ignores_deactivated_duplicate(self) -> None:
        members = [_active_user("UPEER", "peer"), {**_active_user("UDEAD", "peer"), "deleted": True}]

        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _members_page(members)

        assert resolve_unique_active_user_id(get=get, handle="peer") == "UPEER"

    def test_two_active_matches_are_ambiguous_across_pages(self) -> None:
        pages = {
            "": _members_page([_active_user("UONE", "peer")], next_cursor="next"),
            "next": _members_page([_active_user("UTWO", "peer")]),
        }

        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return pages[str(params.get("cursor", ""))]

        assert resolve_unique_active_user_id(get=get, handle="peer") == ""

    def test_bot_guest_and_unknown_status_are_never_a_match(self) -> None:
        members = [
            {**_active_user("UBOT", "peer"), "is_bot": True},
            {**_active_user("UGUEST", "peer"), "is_restricted": True},
            {"id": "UUNKNOWN", "name": "peer", "deleted": False},
        ]

        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _members_page(members)

        assert resolve_unique_active_user_id(get=get, handle="peer") == ""

    def test_unreadable_directory_raises_instead_of_looking_empty(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return {"ok": False, "error": "missing_scope"}

        with pytest.raises(RuntimeError, match="could not be read"):
            resolve_unique_active_user_id(get=get, handle="peer")


class TestResolveUserId:
    def test_empty_handle_short_circuits(self) -> None:
        assert resolve_user_id(get=lambda method, params, *, token="": {}, handle="@") == ""

    def test_email_handle_uses_lookup_by_email(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            assert method == "users.lookupByEmail"
            return {"ok": True, "user": {"id": "U9"}}

        assert resolve_user_id(get=get, handle="alice@example.com") == "U9"

    def test_matches_on_first_page(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _members_page([{"name": "alice", "id": "U1"}])

        assert resolve_user_id(get=get, handle="alice") == "U1"

    def test_follows_cursor_to_a_later_page(self) -> None:
        pages = {
            "": _members_page([{"name": "bob", "id": "U1"}], next_cursor="p2"),
            "p2": _members_page([{"name": "alice", "id": "U2"}]),
        }

        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            assert method == "users.list"
            return pages[str(params.get("cursor", ""))]

        assert resolve_user_id(get=get, handle="@alice") == "U2"

    def test_absent_handle_returns_empty_after_exhausting_pages(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _members_page([{"name": "bob", "id": "U1"}])

        assert resolve_user_id(get=get, handle="alice") == ""

    def test_malformed_members_page_does_not_crash(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return {"ok": True, "members": "nope"}

        assert resolve_user_id(get=get, handle="alice") == ""

    def test_page_cap_bounds_the_walk(self) -> None:
        calls = 0

        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            nonlocal calls
            calls += 1
            return _members_page([{"name": "bob", "id": "U1"}], next_cursor="more")

        assert resolve_user_id(get=get, handle="alice") == ""
        assert calls == _MAX_USER_PAGES


def _usergroups(groups: object) -> RawAPIDict:
    return {"ok": True, "usergroups": groups}


class TestResolveUsergroupId:
    def test_a_group_id_is_returned_without_a_lookup(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            raise AssertionError(method)

        assert resolve_usergroup_id(get=get, handle="S0REVIEWERS") == "S0REVIEWERS"

    def test_a_display_name_is_not_a_handle(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _usergroups([{"id": "S0REVIEWERS", "handle": "reviewers", "name": "Reviewers"}])

        assert resolve_usergroup_id(get=get, handle="Reviewers") == ""

    def test_empty_handle_short_circuits(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            raise AssertionError(method)

        assert resolve_usergroup_id(get=get, handle="@") == ""

    def test_a_refused_listing_resolves_nothing(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return {"ok": False, "error": "missing_scope", "usergroups": [{"id": "S0REVIEWERS", "handle": "reviewers"}]}

        assert resolve_usergroup_id(get=get, handle="reviewers") == ""

    def test_malformed_listing_does_not_crash(self) -> None:
        def get(method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
            return _usergroups(["nope", {"handle": "reviewers", "id": 7}])

        assert resolve_usergroup_id(get=get, handle="reviewers") == ""


def _read_history(get: Getter) -> list[RawAPIDict]:
    return read_channel_history_or_refuse(get=get, channel="D1", token="xoxb-bot", user_token="xoxp-user")


def _read_thread(get: Getter) -> list[RawAPIDict]:
    return read_thread_replies(get=get, channel="D1", thread_ts="1.0", user_token="xoxp-user")


_READERS = [
    pytest.param(
        _read_history,
        "conversations.history",
        "xoxb-bot",
        ChannelReadRefusedError,
        id="history",
    ),
    pytest.param(
        _read_thread,
        "conversations.replies",
        "",
        SlackReadRefusedError,
        id="thread",
    ),
]


@pytest.mark.parametrize(("read", "method", "first_token", "refusal_type"), _READERS)
@pytest.mark.parametrize("error_code", ["not_in_channel", "channel_not_found"])
def test_history_and_thread_retry_membership_refusals(
    read: Callable[[Getter], list[RawAPIDict]],
    method: str,
    first_token: str,
    refusal_type: type[Exception],
    error_code: str,
) -> None:
    tokens: list[str] = []

    def get(request_method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
        assert request_method == method
        assert params["channel"] == "D1"
        tokens.append(token)
        if token == first_token:
            return {"ok": False, "error": error_code}
        return {"ok": True, "messages": [{"ts": "1.0"}]}

    assert read(get) == [{"ts": "1.0", "channel": "D1"}]
    assert tokens == [first_token, "xoxp-user"]


@pytest.mark.parametrize(("read", "method", "first_token", "refusal_type"), _READERS)
def test_history_and_thread_preserve_refusal_type(
    read: Callable[[Getter], list[RawAPIDict]],
    method: str,
    first_token: str,
    refusal_type: type[Exception],
) -> None:
    tokens: list[str] = []

    def get(request_method: str, params: dict[str, str | int], *, token: str = "") -> RawAPIDict:
        assert request_method == method
        assert params["channel"] == "D1"
        tokens.append(token)
        return {"ok": False, "error": "is_archived"}

    with pytest.raises(refusal_type) as refused:
        read(get)

    assert type(refused.value) is refusal_type
    assert tokens == [first_token]
