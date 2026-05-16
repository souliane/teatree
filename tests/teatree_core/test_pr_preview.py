"""Tests for the PR ship-preview / metadata helpers (mirrors ``_pr_preview``).

Split out of ``test_pr_command`` alongside the ``_pr_preview`` module split:
test files mirror the production module path.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.management.commands import _pr_preview
from teatree.core.management.commands._pr_preview import ship_preview, slug_from_remote
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


class TestShipPreviewTitleDescriptionInvariant(TestCase):
    """The description's first line must always equal the title.

    A diverged title vs. description-first-line is what blocks the
    release-notes pipeline. ``ship_preview`` must build them so they can
    never differ by construction, regardless of body presence, the
    fallback title path, or close-keyword sanitization.
    """

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="test",
            state=Ticket.State.REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/119",
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        return ticket

    def _first_line(self, description: str) -> str:
        return description.split("\n", 1)[0]

    def test_first_line_equals_title_with_body(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("feat: add X [FLAG] (proj#119)", "Body paragraph.\n"),
            ),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        assert self._first_line(description) == title

    def test_first_line_equals_title_without_body(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(_pr_preview.git, "last_commit_message", return_value=("fix: y (proj#119)", "")),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        assert self._first_line(description) == title
        assert title == "fix: y (proj#119)"

    def test_first_line_equals_title_on_fallback_title(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(_pr_preview.git, "last_commit_message", return_value=("", "")),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        # Invariant holds even when the fallback title carries a close
        # keyword ("Resolve") that close-keyword sanitization rewrites.
        assert self._first_line(description) == title
        assert ticket.issue_url in title

    def test_first_line_equals_title_when_subject_has_close_keyword(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("fix: resolves #119 the corridor bug (proj#119)", "Body."),
            ),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        # Title and first line are both the *sanitized* string -> still equal.
        assert self._first_line(description) == title


class TestSlugFromRemote(TestCase):
    def test_github_ssh(self) -> None:
        assert slug_from_remote("git@github.com:souliane/teatree.git") == "souliane/teatree"

    def test_github_https(self) -> None:
        assert slug_from_remote("https://github.com/souliane/teatree.git") == "souliane/teatree"

    def test_gitlab_nested_namespace(self) -> None:
        assert slug_from_remote("git@gitlab.com:acme/team/backend.git") == "acme/team/backend"

    def test_no_dot_git_suffix(self) -> None:
        assert slug_from_remote("https://github.com/souliane/teatree") == "souliane/teatree"

    def test_empty_returns_empty(self) -> None:
        assert slug_from_remote("") == ""
