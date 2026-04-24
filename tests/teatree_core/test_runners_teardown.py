"""Tests for WorktreeTeardown — composed runner for the mark_merged transition.

Stage 5 of #140: ``Ticket.mark_merged()`` becomes a thin transition that
enqueues teardown I/O (worktree removal, branch deletion, DB drop) onto a
``@task`` worker. The worker invokes ``WorktreeTeardown`` and on success
the ticket is ready for ``retrospect()``.
"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from teatree.core.runners import WorktreeTeardown
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def _clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestWorktreeTeardown(TestCase):
    def _ticket_with_worktrees(self, count: int = 2) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        for i in range(count):
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path=f"repo-{i}",
                branch="feat-x",
                extra={"worktree_path": f"/tmp/wt-{i}"},
            )
        return ticket

    def test_returns_success_when_no_worktrees(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/78")

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is True
        assert "no worktrees" in result.detail.lower()

    def test_cleans_each_worktree_and_returns_summary(self) -> None:
        ticket = self._ticket_with_worktrees(count=2)

        cleaned: list[str] = []

        def fake_cleanup(worktree: Worktree, *, force: bool = False) -> str:
            del force
            label = f"Cleaned: {worktree.repo_path}"
            cleaned.append(worktree.repo_path)
            worktree.delete()
            return label

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.teardown.cleanup_worktree", side_effect=fake_cleanup),
        ):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is True
        assert sorted(cleaned) == ["repo-0", "repo-1"]
        assert ticket.worktrees.count() == 0

    def test_continues_on_individual_failure_and_reports_errors(self) -> None:
        ticket = self._ticket_with_worktrees(count=2)

        def fake_cleanup(worktree: Worktree, *, force: bool = False) -> str:
            del force
            if worktree.repo_path == "repo-0":
                msg = "branch ahead of main"
                raise RuntimeError(msg)
            worktree.delete()
            return f"Cleaned: {worktree.repo_path}"

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.runners.teardown.cleanup_worktree", side_effect=fake_cleanup),
        ):
            result = WorktreeTeardown(ticket).run()

        assert result.ok is False
        assert "repo-0" in result.detail
        assert "branch ahead of main" in result.detail
        # repo-1 cleaned even though repo-0 raised
        assert ticket.worktrees.filter(repo_path="repo-1").count() == 0
