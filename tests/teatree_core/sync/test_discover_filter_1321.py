"""``discover-mrs`` keeps the user's own open MRs (#1321 regression).

The original #1321 fix wired the 4 skip-condition predicate into
``discover_mrs``. That call site consumes ``host.list_my_prs(author=author)``
which returns ONLY the user's own open PRs — so applying the predicate (whose
first skip is ``author_is_self``) collapsed the output to empty and broke the
own-MR review-request flow ``discover-mrs`` backs.

The predicate belongs on the colleague-MR review-sweep path (see
:mod:`teatree.loop.scanners.reviewer_prs`), not here. These tests pin that
``discover-mrs`` returns own MRs regardless of self-authorship, self-approval,
or other skip signals that only make sense when classifying a colleague's MR.
"""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import followup as followup_command
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY


class TestDiscoverMrsKeepsOwnMrs(TestCase):
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

    def test_own_authored_open_mr_is_returned(self) -> None:
        """The fundamental input to ``list_my_prs`` is own MRs — discover MUST keep them."""
        prs = [
            {
                "number": 1,
                "title": "my open feature",
                "html_url": "https://example.com/o/r/pull/1",
                "user": {"login": "souliane"},
                "state": "open",
            },
        ]
        result = self._discover(prs, user="souliane")
        assert result["count"] == 1
        mr = cast("list[dict[str, object]]", result["mrs"])[0]
        assert mr["iid"] == 1

    def test_own_mr_with_self_in_approvers_is_returned(self) -> None:
        """Self-approval on an own MR (allowed in some configs) MUST NOT filter it out."""
        prs = [
            {
                "number": 2,
                "title": "my approved feature",
                "html_url": "https://example.com/o/r/pull/2",
                "user": {"login": "souliane"},
                "state": "open",
                "approvers": [{"username": "souliane"}],
            },
        ]
        result = self._discover(prs, user="souliane")
        assert result["count"] == 1

    def test_own_mr_with_self_authored_note_is_returned(self) -> None:
        """A self-authored note on an own MR MUST NOT filter it out."""
        prs = [
            {
                "number": 3,
                "title": "my MR with my note",
                "html_url": "https://example.com/o/r/pull/3",
                "user": {"login": "souliane"},
                "state": "open",
                "notes": [
                    {"author": {"login": "souliane"}, "system": False, "body": "self comment"},
                ],
            },
        ]
        result = self._discover(prs, user="souliane")
        assert result["count"] == 1

    def test_draft_own_mr_is_still_filtered(self) -> None:
        """Draft filtering is unchanged — ``_is_draft`` predates #1321 and stays in place."""
        prs = [
            {
                "number": 4,
                "title": "draft",
                "html_url": "https://example.com/o/r/pull/4",
                "user": {"login": "souliane"},
                "state": "open",
                "draft": True,
            },
        ]
        result = self._discover(prs, user="souliane")
        assert result["count"] == 0
