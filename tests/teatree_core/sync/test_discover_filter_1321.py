"""``discover-mrs`` enforces the 4 review-candidate skip-conditions (#1321).

The auto-sweep used to surface MRs the agent should not have re-reviewed
(own MRs incorrectly listed as colleague candidates, already-approved MRs,
already-merged broadcasts, broadcasts another engineer had reacted to).
The CLI now applies the shared predicate from
:mod:`teatree.core.review_candidate` before returning the list.
"""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import followup as followup_command
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY


class TestDiscoverFilters1321(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def _discover(self, prs: list[dict[str, object]], *, user: str = "souliane") -> dict[str, object]:
        host = MagicMock()
        host.current_user.return_value = user
        host.list_my_prs.return_value = prs
        self._monkeypatch.setattr(followup_command, "code_host_from_overlay", lambda: host)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            return cast("dict[str, object]", call_command("followup", "discover-mrs"))

    def test_skip_condition_1_drops_mr_authored_by_self(self) -> None:
        """An MR whose explicit author matches the current user is filtered (#1321 condition 1)."""
        prs = [
            {
                "number": 1,
                "title": "self-authored",
                "html_url": "https://example.com/o/r/pull/1",
                "user": {"login": "souliane"},
                "state": "open",
            },
        ]
        result = self._discover(prs, user="souliane")
        assert result["count"] == 0

    def test_skip_condition_2_drops_mr_already_approved_by_self(self) -> None:
        prs = [
            {
                "number": 2,
                "title": "approved-by-self",
                "html_url": "https://example.com/o/r/pull/2",
                "state": "open",
                "approved": True,
                "approvers": [{"username": "souliane"}],
            },
        ]
        result = self._discover(prs)
        assert result["count"] == 0

    def test_skip_condition_2b_drops_mr_with_self_authored_non_system_note(self) -> None:
        prs = [
            {
                "number": 3,
                "title": "has-my-note",
                "html_url": "https://example.com/o/r/pull/3",
                "state": "open",
                "notes": [
                    {"author": {"login": "souliane"}, "system": False, "body": "I already reviewed"},
                ],
            },
        ]
        result = self._discover(prs)
        assert result["count"] == 0

    def test_skip_condition_3_drops_merged_mr(self) -> None:
        prs = [
            {
                "number": 4,
                "title": "merged",
                "html_url": "https://example.com/o/r/pull/4",
                "state": "merged",
            },
        ]
        result = self._discover(prs)
        assert result["count"] == 0

    def test_skip_condition_3b_drops_closed_mr(self) -> None:
        prs = [
            {
                "number": 5,
                "title": "closed",
                "html_url": "https://example.com/o/r/pull/5",
                "state": "closed",
            },
        ]
        result = self._discover(prs)
        assert result["count"] == 0

    def test_open_clean_mr_without_skip_signals_is_kept(self) -> None:
        prs = [
            {
                "number": 9,
                "title": "clean",
                "html_url": "https://example.com/o/r/pull/9",
                "state": "open",
            },
        ]
        result = self._discover(prs)
        assert result["count"] == 1
        mr = cast("list[dict[str, object]]", result["mrs"])[0]
        assert mr["iid"] == 9
