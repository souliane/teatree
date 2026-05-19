"""``followup discover-mrs`` — enumerate the user's open non-draft PRs/MRs.

Regression for souliane/teatree#1008: ``t3 review-request discover``
delegated to ``manage.py followup discover-mrs``, a subcommand that did
not exist, so the documented review-request discovery flow was dead.
"""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import followup as followup_command
from tests.teatree_core.conftest import CommandOverlay
from tests.teatree_core.pr_command._shared import _MOCK_OVERLAY


class TestDiscoverMrs(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_returns_open_non_draft_prs_for_authenticated_user(self) -> None:
        from teatree.core.overlay import OverlayConfig  # noqa: PLC0415

        host = MagicMock()
        host.list_my_prs.return_value = [
            {
                "iid": 1,
                "title": "feat: x",
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/1",
                "references": {"full": "org/repo!1"},
            },
            {
                "iid": 2,
                "title": "fix: y (draft)",
                "web_url": "https://gitlab.com/org/other/-/merge_requests/2",
                "references": {"full": "org/other!2"},
                "draft": True,
            },
        ]
        self._monkeypatch.setattr(followup_command, "code_host_from_overlay", lambda: host)
        overlay = CommandOverlay()
        # Per-instance config so we don't mutate the class-level default shared by other tests.
        overlay.config = OverlayConfig()
        overlay.config.get_gitlab_username = lambda: "adrien"  # type: ignore[method-assign]

        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}):
            result = cast("dict[str, object]", call_command("followup", "discover-mrs"))

        assert result["author"] == "adrien"
        assert result["count"] == 1
        mrs = cast("list[dict[str, object]]", result["mrs"])
        assert len(mrs) == 1
        assert mrs[0]["iid"] == 1
        assert mrs[0]["repo"] == "org/repo"
        assert mrs[0]["title"] == "feat: x"
        assert mrs[0]["url"] == "https://gitlab.com/org/repo/-/merge_requests/1"
        host.list_my_prs.assert_called_once_with(author="adrien")

    def test_resolves_github_repo_slug_from_repository_url(self) -> None:
        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.list_my_prs.return_value = [
            {
                "number": 7,
                "title": "chore: z",
                "html_url": "https://github.com/souliane/teatree/pull/7",
                "repository_url": "https://api.github.com/repos/souliane/teatree",
            },
        ]
        self._monkeypatch.setattr(followup_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("followup", "discover-mrs"))

        mrs = cast("list[dict[str, object]]", result["mrs"])
        assert mrs[0]["repo"] == "souliane/teatree"
        assert mrs[0]["iid"] == 7
        assert mrs[0]["url"] == "https://github.com/souliane/teatree/pull/7"

    def test_falls_back_to_current_user_when_no_username_configured(self) -> None:
        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.list_my_prs.return_value = []
        self._monkeypatch.setattr(followup_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("followup", "discover-mrs"))

        assert result["author"] == "souliane"
        assert result["count"] == 0
        assert result["mrs"] == []
        host.list_my_prs.assert_called_once_with(author="souliane")

    def test_returns_error_when_no_code_host_configured(self) -> None:
        self._monkeypatch.setattr(followup_command, "code_host_from_overlay", lambda: None)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("followup", "discover-mrs"))

        assert "error" in result

    def test_returns_error_when_username_unresolved(self) -> None:
        host = MagicMock()
        host.current_user.return_value = ""
        self._monkeypatch.setattr(followup_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("followup", "discover-mrs"))

        assert "error" in result
        host.list_my_prs.assert_not_called()


class TestRepoSlug:
    """Pure parsing of ``owner/name`` from heterogeneous PR/MR shapes."""

    def test_gitlab_url_fallback_when_no_references(self) -> None:
        slug = followup_command._repo_slug(
            {"web_url": "https://gitlab.com/org/sub/repo/-/merge_requests/9"},
        )
        assert slug == "org/sub/repo"

    def test_empty_when_no_recognisable_fields(self) -> None:
        assert followup_command._repo_slug({"title": "no urls here"}) == ""

    def test_references_dict_without_full_falls_through(self) -> None:
        slug = followup_command._repo_slug(
            {"references": {}, "web_url": "https://gitlab.com/org/repo/-/merge_requests/3"},
        )
        assert slug == "org/repo"


class TestFieldExtractors:
    """``_str_field`` / ``_int_field`` no-match fallbacks."""

    def test_str_field_returns_empty_when_absent(self) -> None:
        assert followup_command._str_field({"a": "x"}, "b", "c") == ""

    def test_int_field_returns_zero_when_absent(self) -> None:
        assert followup_command._int_field({"a": 1}, "b", "c") == 0
