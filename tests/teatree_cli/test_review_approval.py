"""Unit tests for ``teatree.cli.review.approval`` helpers (#1019, #1029).

The functions are exercised end-to-end in ``test_review_approve_gate.py``
and ``test_review_approve_already_approved.py``, but the branches around
malformed payloads and the username-resolution failure mode are easier
to pin directly. Stub against the ``GitLabAPI.current_username`` /
``get_json`` / ``get_json_paginated`` shape — no network, no migrations.

Since #2716 the "no published note" path also consults the recorded
internal verdict (:class:`OnBehalfApproval`), so this module carries the
``django_db`` mark; the note-found tests short-circuit before the DB read
and never write a row.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from teatree.cli.review.approval import identity_has_reviewed, identity_in_approved_by

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _api(*, username: str = "souliane", discussions: Any = None) -> MagicMock:
    api = MagicMock()
    api.current_username.return_value = username
    # identity_has_reviewed uses get_json_paginated; identity_in_approved_by uses get_json.
    resolved = discussions if discussions is not None else []
    api.get_json_paginated.return_value = resolved if isinstance(resolved, list) else []
    api.get_json.return_value = resolved
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
        api.get_json_paginated.assert_not_called()

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


class TestIdentityInApprovedBy:
    """Distinguish GitLab's idempotent already-approved 401 from a real one (#1029)."""

    def test_true_when_identity_in_approved_by(self) -> None:
        api = _api(
            username="souliane",
            discussions={"approved_by": [{"user": {"username": "souliane"}}]},
        )
        assert identity_in_approved_by(api, "org%2Frepo", 7) is True

    def test_false_when_identity_not_in_approved_by(self) -> None:
        api = _api(
            username="souliane",
            discussions={"approved_by": [{"user": {"username": "someone-else"}}]},
        )
        assert identity_in_approved_by(api, "org%2Frepo", 7) is False

    def test_false_when_username_unresolved(self) -> None:
        # An unresolvable identity can't be matched — fail closed so a
        # genuine auth failure still surfaces.
        api = _api(username="")
        assert identity_in_approved_by(api, "org%2Frepo", 7) is False
        api.get_json.assert_not_called()

    def test_false_when_approvals_payload_not_a_dict(self) -> None:
        api = _api(username="souliane", discussions=["garbage"])
        assert identity_in_approved_by(api, "org%2Frepo", 7) is False

    def test_false_when_approved_by_not_a_list(self) -> None:
        api = _api(username="souliane", discussions={"approved_by": "nope"})
        assert identity_in_approved_by(api, "org%2Frepo", 7) is False

    def test_skips_non_dict_and_non_dict_user_entries(self) -> None:
        api = _api(
            username="souliane",
            discussions={
                "approved_by": [
                    None,
                    "garbage",
                    {"user": "not-a-dict"},
                    {"user": {"username": "souliane"}},
                ],
            },
        )
        assert identity_in_approved_by(api, "org%2Frepo", 7) is True
