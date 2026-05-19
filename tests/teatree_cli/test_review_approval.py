"""Unit tests for ``teatree.cli.review_approval.identity_has_reviewed`` (#1019).

The function is exercised end-to-end in ``test_review_approve_gate.py``,
but the branches around malformed payloads and the username-resolution
failure mode are easier to pin directly. Pure stub against the
``GitLabAPI.current_username`` / ``get_json`` shape — no Django, no
network, no migrations.
"""

from typing import Any
from unittest.mock import MagicMock

from teatree.cli.review_approval import identity_has_reviewed


def _api(*, username: str = "souliane", discussions: Any = None) -> MagicMock:
    api = MagicMock()
    api.current_username.return_value = username
    api.get_json.return_value = discussions if discussions is not None else []
    return api


class TestIdentityHasReviewed:
    """Coverage for review-before-approve precondition resolution."""

    def test_returns_false_with_error_when_username_resolution_fails(self) -> None:
        # An empty username is a hard precondition failure — the function
        # must surface the diagnostic without scanning discussions.
        api = _api(username="")
        reviewed, error = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is False
        assert error  # non-empty diagnostic
        assert "identity" in error.lower() or "token" in error.lower()
        api.get_json.assert_not_called()

    def test_returns_true_when_identity_authored_a_note(self) -> None:
        discussions = [
            {
                "notes": [
                    {"author": {"username": "other"}},
                    {"author": {"username": "souliane"}},
                ],
            },
        ]
        api = _api(username="souliane", discussions=discussions)
        reviewed, error = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is True
        assert error == ""

    def test_returns_false_when_no_authored_notes_found(self) -> None:
        discussions = [
            {"notes": [{"author": {"username": "other"}}]},
        ]
        api = _api(username="souliane", discussions=discussions)
        reviewed, error = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is False
        assert error == ""

    def test_returns_false_when_get_json_returns_non_list(self) -> None:
        # Malformed payload (dict instead of list of discussions): treat as
        # "no review" rather than raising.
        api = _api(username="souliane", discussions={"error": "boom"})
        reviewed, error = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is False
        assert error == ""

    def test_skips_non_dict_discussion_entries(self) -> None:
        # GitLab pagination quirks can interleave None/strings; guard.
        discussions: list[Any] = [
            None,
            "garbage",
            42,
            {"notes": [{"author": {"username": "souliane"}}]},
        ]
        api = _api(username="souliane", discussions=discussions)
        reviewed, _ = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is True

    def test_skips_discussions_without_notes_list(self) -> None:
        discussions: list[Any] = [
            {"id": "abc"},  # no notes key
            {"notes": "not a list"},
            {"notes": [{"author": {"username": "souliane"}}]},
        ]
        api = _api(username="souliane", discussions=discussions)
        reviewed, _ = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is True

    def test_skips_non_dict_note_entries(self) -> None:
        discussions = [
            {"notes": ["string", 42, None, {"author": {"username": "souliane"}}]},
        ]
        api = _api(username="souliane", discussions=discussions)
        reviewed, _ = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is True

    def test_skips_notes_with_non_dict_author(self) -> None:
        discussions = [
            {
                "notes": [
                    {"author": "souliane"},  # author should be a dict
                    {"author": None},
                    {"author": {"username": "souliane"}},
                ],
            },
        ]
        api = _api(username="souliane", discussions=discussions)
        reviewed, _ = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is True

    def test_username_mismatch_returns_false(self) -> None:
        discussions = [
            {"notes": [{"author": {"username": "bob"}}]},
        ]
        api = _api(username="alice", discussions=discussions)
        reviewed, error = identity_has_reviewed(api, "org%2Frepo", 1)
        assert reviewed is False
        assert error == ""
