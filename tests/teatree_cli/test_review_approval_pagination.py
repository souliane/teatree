"""M5 regression: ``identity_has_reviewed`` must paginate discussions.

``get_json`` silently truncates to a single page (at most 100 threads).
A reviewer whose note lands on page 2+ would read as "not reviewed",
causing ``approve`` to refuse with the review-before-approve error even
though the reviewer already left a note.

This test reproduces the bug: stub ``get_json_paginated`` to return two
pages where the reviewer's note is only on page 2, then assert the
function returns ``(True, "")``.

Severity analysis (single caller): ``ReviewService.approve``
(``teatree/cli/review/service.py`` ~line 516). The wrong ``(False, "")`` hits
the ``if not reviewed`` branch and returns ``rc=1`` with "Refusing to
approve" — the approve is BLOCKED, not silently granted. Fail-CLOSED
(over-blocks); no maker≠checker hole. The fix lets a legitimate
reviewer approve.
"""

from unittest.mock import MagicMock

import pytest

from teatree.cli.review.approval import identity_has_reviewed

# The reviewer-absent path consults the recorded internal verdict
# (:class:`OnBehalfApproval`) since #2716, so it touches the DB.
# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _api_with_paginated(*, username: str, page1: list, page2: list) -> MagicMock:
    """Return a mock that has both ``get_json_paginated`` and ``get_json``."""
    api = MagicMock()
    api.current_username.return_value = username
    # get_json_paginated must return the full flattened list across pages.
    # Here we simulate the fixed code calling get_json_paginated with all items.
    api.get_json_paginated.return_value = page1 + page2
    # get_json returns only page1 (the old broken behaviour).
    api.get_json.return_value = page1
    return api


class TestIdentityHasReviewedPagination:
    """Reviewer on page 2 is found only when all pages are fetched."""

    def test_finds_reviewer_note_on_page_two(self) -> None:
        page1 = [
            {"notes": [{"author": {"username": "other-reviewer"}}]},
        ] * 100  # fill page 1 with someone else's notes
        page2 = [
            {"notes": [{"author": {"username": "souliane"}}]},
        ]
        api = _api_with_paginated(username="souliane", page1=page1, page2=page2)

        reviewed, error = identity_has_reviewed(api, "org%2Frepo", 1)

        assert reviewed is True, "reviewer's note is on page 2 — must be found when all pages are fetched"
        assert error == ""
        # The paginated method must be used (not the single-page get_json).
        api.get_json_paginated.assert_called_once()
        api.get_json.assert_not_called()

    def test_returns_false_when_reviewer_absent_across_all_pages(self) -> None:
        page1 = [{"notes": [{"author": {"username": "other"}}]}] * 100
        page2 = [{"notes": [{"author": {"username": "also-other"}}]}]
        api = _api_with_paginated(username="souliane", page1=page1, page2=page2)

        reviewed, error = identity_has_reviewed(api, "org%2Frepo", 1)

        assert reviewed is False
        assert error == ""
