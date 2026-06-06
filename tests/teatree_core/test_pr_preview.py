"""Tests for the PR ship-preview / metadata helpers (mirrors ``_pr_preview``).

Split out of ``test_pr_command`` alongside the ``_pr_preview`` module split:
test files mirror the production module path.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.management.commands import _pr_preview
from teatree.core.management.commands._pr_preview import ship_preview
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay import OverlayMetadata
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}


class _GeneratingMetadata(OverlayMetadata):
    """An overlay metadata that REPLACES a non-canonical subject with a fixed title."""

    def build_pr_title(self, *, branch: str, subject: str, body: str, issue_url: str) -> str:
        return "fix(corridor): canonical generated title [none] (proj#119)"


class _GenOverlay(CommandOverlay):
    metadata = _GeneratingMetadata()


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


class TestShipPreviewUsesOverlayGeneratedTitle(TestCase):
    """The title is PRODUCED by the overlay, not copied from the subject.

    An overlay enforcing a title grammar must be able to REPLACE a
    non-canonical commit subject (e.g. ``test(insurance): …``) with a
    compliant generated title — and the description first line must follow it
    so the two never diverge.
    """

    def _ticket_with_worktree(self) -> Ticket:
        ticket = Ticket.objects.create(
            overlay="gen",
            state=Ticket.State.REVIEWED,
            issue_url="https://github.com/souliane/teatree/issues/119",
        )
        Worktree.objects.create(
            ticket=ticket,
            overlay="gen",
            repo_path="/tmp/backend",
            branch="119-fix-corridor-margin",
            extra={"worktree_path": "/tmp/backend"},
        )
        return ticket

    def test_overlay_generated_title_replaces_subject_and_first_line_follows(self) -> None:
        ticket = self._ticket_with_worktree()
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value={"gen": _GenOverlay()}),
            patch.object(
                _pr_preview.git,
                "last_commit_message",
                return_value=("test(insurance): add coverage", "Body paragraph.\n"),
            ),
        ):
            _, title, description = ship_preview(ticket, ticket.worktrees.first())
        assert title == "fix(corridor): canonical generated title [none] (proj#119)"
        assert description.split("\n", 1)[0] == title
