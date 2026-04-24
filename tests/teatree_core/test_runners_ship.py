"""Tests for ShipExecutor — composed runner for the ship transition.

Stage 2 of #140: ``Ticket.ship()`` becomes a thin transition that enqueues
the heavy I/O (push, MR creation) onto a ``@task`` worker. The worker runs
``ShipExecutor`` and on success advances ``SHIPPED → IN_REVIEW``.
"""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.core.runners import ShipExecutor
from teatree.core.runners.ship import overlay_mr_labels, sanitize_close_keywords
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestShipExecutor(TestCase):
    def _ticket_with_worktree(self, *, branch: str = "feat-x", repo: str = "/tmp/repo") -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path=repo,
            branch=branch,
            extra={"worktree_path": repo},
        )
        return ticket

    def test_pushes_branch_then_creates_pr_and_records_url(self) -> None:
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "https://example.com/mr/1", "iid": 1}
        host.current_user.return_value = "souliane"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push") as push,
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat: x", "body")),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is True
        push.assert_called_once_with(repo="/tmp/repo", remote="origin", branch="feat-x")
        (spec,) = host.create_pr.call_args.args
        assert spec.repo == "/tmp/repo"
        assert spec.branch == "feat-x"
        assert spec.title == "feat: x"
        assert spec.assignee == "souliane"

        ticket.refresh_from_db()
        assert ticket.extra["mr_urls"] == ["https://example.com/mr/1"]

    def test_returns_failure_when_no_code_host(self) -> None:
        ticket = self._ticket_with_worktree()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=None),
            patch("teatree.core.runners.ship.git.push"),
        ):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "code host" in result.detail.lower()

    def test_returns_failure_when_no_worktree(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/78")

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = ShipExecutor(ticket).run()

        assert result.ok is False
        assert "worktree" in result.detail.lower()

    def test_assignee_falls_back_to_git_user_name_when_host_returns_empty(self) -> None:
        ticket = self._ticket_with_worktree()
        host = MagicMock()
        host.create_pr.return_value = {"web_url": "u"}
        host.current_user.return_value = ""

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.ship.code_host_from_overlay", return_value=host),
            patch("teatree.core.runners.ship.git.push"),
            patch("teatree.core.runners.ship.git.last_commit_message", return_value=("feat", "")),
            patch("teatree.core.runners.ship.git.config_value", return_value="dev"),
        ):
            ShipExecutor(ticket).run()

        (spec,) = host.create_pr.call_args.args
        assert spec.assignee == "dev"


class TestSanitizeCloseKeywords:
    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("Closes #123", "Relates to #123"),
            ("Fixes #42", "Relates to #42"),
            ("Resolves #7", "Relates to #7"),
            ("closes #123", "Relates to #123"),
            ("See Closes #1 and Fixes #2", "See Relates to #1 and Relates to #2"),
            ("Closes group/project#99", "Relates to group/project#99"),
            (
                "Closes https://gitlab.com/org/project/-/issues/729",
                "Relates to https://gitlab.com/org/project/-/issues/729",
            ),
            (
                "Resolves https://github.com/owner/repo/issues/10",
                "Relates to https://github.com/owner/repo/issues/10",
            ),
            ("No ticket ref here", "No ticket ref here"),
            ("", ""),
        ],
    )
    def test_replaces_close_keywords_when_close_ticket_false(self, description: str, expected: str) -> None:
        assert sanitize_close_keywords(description, close_ticket=False) == expected

    def test_leaves_description_unchanged_when_close_ticket_true(self) -> None:
        assert sanitize_close_keywords("Closes #123", close_ticket=True) == "Closes #123"


class TestOverlayMrLabels:
    def test_default_overlay_returns_empty(self) -> None:
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            assert overlay_mr_labels() == []

    def test_overlay_with_string_labels(self) -> None:
        mock = MagicMock()
        mock.config.mr_auto_labels = "label-a, label-b"
        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": mock}):
            assert overlay_mr_labels() == ["label-a", "label-b"]

    def test_non_iterable_returns_empty(self) -> None:
        mock = MagicMock()
        mock.config.mr_auto_labels = 42
        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": mock}):
            assert overlay_mr_labels() == []
