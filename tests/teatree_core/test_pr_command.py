from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command

from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands.pr import _mr_auto_labels
from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestPrCreate:
    @pytest.mark.django_db
    def test_reads_auto_labels_from_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host = MagicMock()
        host.create_pr.return_value = {"iid": 12}
        monkeypatch.setattr(pr_command, "get_code_host", lambda: host)

        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/55")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/backend", branch="feature-branch")

        # CommandOverlay.get_mr_auto_labels() returns [] (default), so labels=None
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "create", str(ticket.id), "--title", "feat: add labels")

        assert result == {"iid": 12}
        host.create_pr.assert_called_once_with(
            repo="/tmp/backend",
            branch="feature-branch",
            title="feat: add labels",
            description="",
            labels=None,
        )


class TestPostEvidence:
    @pytest.mark.django_db
    def test_delegates_to_code_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """post-evidence posts an MR note via the code host."""
        host = MagicMock()
        host.post_mr_note.return_value = {"id": 55}
        monkeypatch.setattr(pr_command, "get_code_host", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "post-evidence", "10", "--body", "All tests pass")

        assert result == {"id": 55}
        host.post_mr_note.assert_called_once()
        call_kw = host.post_mr_note.call_args
        assert call_kw.kwargs["mr_iid"] == 10
        assert "All tests pass" in call_kw.kwargs["body"]

    @pytest.mark.django_db
    def test_returns_error_without_code_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """post-evidence returns error when no code host configured."""
        monkeypatch.setattr(pr_command, "get_code_host", lambda: None)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "post-evidence", "10")

        assert "error" in result


class TestMrAutoLabels:
    def test_default_overlay_returns_empty(self) -> None:
        """CommandOverlay has no auto labels, so _mr_auto_labels returns []."""
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = _mr_auto_labels()
            assert result == []

    def test_overlay_with_string_labels(self) -> None:
        """When overlay returns a comma-separated string, it's split."""
        mock_overlay = MagicMock()
        mock_overlay.config.get_mr_auto_labels.return_value = "label-a, label-b"
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": mock_overlay},
        ):
            result = _mr_auto_labels()
            assert result == ["label-a", "label-b"]

    def test_non_iterable_returns_empty(self) -> None:
        """_mr_auto_labels returns [] for non-iterable value."""
        mock_overlay = MagicMock()
        mock_overlay.config.get_mr_auto_labels.return_value = 42
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": mock_overlay},
        ):
            result = _mr_auto_labels()
            assert result == []
