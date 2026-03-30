from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from django.core.management import call_command
from django.test import override_settings

from teetree.core.management.commands import pr as pr_command
from teetree.core.management.commands.pr import _mr_auto_labels
from teetree.core.models import Ticket, Worktree
from teetree.core.overlay_loader import reset_overlay_cache


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


class TestPrCreate:
    @override_settings(
        TEATREE_OVERLAY_CLASS="tests.teetree_core.test_management_commands.CommandOverlay",
        TEATREE_MR_AUTO_LABELS=["Process::Technical review", "customer::foo"],
    )
    @pytest.mark.django_db
    def test_reads_auto_labels_from_django_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host = MagicMock()
        host.create_pr.return_value = {"iid": 12}
        monkeypatch.setattr(pr_command, "get_code_host", lambda: host)

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/55")
        Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="feature-branch")

        result = call_command("pr", "create", str(ticket.id), "--title", "feat: add labels")

        assert result == {"iid": 12}
        host.create_pr.assert_called_once_with(
            repo="/tmp/backend",
            branch="feature-branch",
            title="feat: add labels",
            description="",
            labels=["Process::Technical review", "customer::foo"],
        )


class TestPostEvidence:
    @override_settings(
        TEATREE_OVERLAY_CLASS="tests.teetree_core.test_management_commands.CommandOverlay",
    )
    @pytest.mark.django_db
    def test_delegates_to_code_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """post-evidence posts an MR note via the code host (line 93 -- repo fallback, line 97)."""
        host = MagicMock()
        host.post_mr_note.return_value = {"id": 55}
        monkeypatch.setattr(pr_command, "get_code_host", lambda: host)

        result = call_command("pr", "post-evidence", "10", "--body", "All tests pass")

        assert result == {"id": 55}
        host.post_mr_note.assert_called_once()
        call_kw = host.post_mr_note.call_args
        assert call_kw.kwargs["mr_iid"] == 10
        assert "All tests pass" in call_kw.kwargs["body"]

    @override_settings(
        TEATREE_OVERLAY_CLASS="tests.teetree_core.test_management_commands.CommandOverlay",
    )
    @pytest.mark.django_db
    def test_returns_error_without_code_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """post-evidence returns error when no code host configured."""
        monkeypatch.setattr(pr_command, "get_code_host", lambda: None)

        result = call_command("pr", "post-evidence", "10")

        assert "error" in result


class TestMrAutoLabels:
    @override_settings(TEATREE_MR_AUTO_LABELS="label-a, label-b")
    def test_from_comma_separated_string(self) -> None:
        """_mr_auto_labels splits comma-separated string (line 93)."""
        result = _mr_auto_labels()
        assert result == ["label-a", "label-b"]

    @override_settings(TEATREE_MR_AUTO_LABELS=42)
    def test_non_iterable_returns_empty(self) -> None:
        """_mr_auto_labels returns [] for non-iterable value (line 97)."""
        result = _mr_auto_labels()
        assert result == []
