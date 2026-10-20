"""Branch coverage for the web_reads free functions: the paged user lookup (#3507) and the user-group lookup."""

import pytest

from teatree.backends.slack.web_reads import (
    _MAX_USER_PAGES,
    resolve_unique_active_user_id,
    resolve_user_id,
    resolve_usergroup_id,
)
from teatree.types import RawAPIDict


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
    def test_unique_active_match_ignores_deactivated_duplicate(self) -> None:
        members = [_active_user("UPEER", "peer"), {"id": "UDEAD", "name": "peer", "deleted": True}]

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
