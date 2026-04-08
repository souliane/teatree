from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands.pr import _check_shipping_gate, _mr_auto_labels
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


class TestPrCreate(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_reads_auto_labels_from_overlay(self) -> None:
        host = MagicMock()
        host.create_pr.return_value = {"iid": 12}
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/55")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/backend", branch="feature-branch")

        # CommandOverlay.get_mr_auto_labels() returns [] (default), so labels=None
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.management.commands.pr._last_commit_message", return_value=("", "")),
        ):
            result = call_command("pr", "create", str(ticket.id), "--title", "feat: add labels")

        assert result == {"iid": 12}
        call_kwargs = host.create_pr.call_args.kwargs
        assert call_kwargs["repo"] == "/tmp/backend"
        assert call_kwargs["branch"] == "feature-branch"
        assert call_kwargs["title"] == "feat: add labels"
        assert "assignee" in call_kwargs


class TestPostEvidence(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_delegates_to_code_host(self) -> None:
        """post-evidence posts an MR note via the code host."""
        host = MagicMock()
        host.post_mr_note.return_value = {"id": 55}
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "post-evidence", "10", "--body", "All tests pass")

        assert result == {"id": 55}
        host.post_mr_note.assert_called_once()
        call_kw = host.post_mr_note.call_args
        assert call_kw.kwargs["mr_iid"] == 10
        assert "All tests pass" in call_kw.kwargs["body"]

    def test_returns_error_without_code_host(self) -> None:
        """post-evidence returns error when no code host configured."""
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: None)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = call_command("pr", "post-evidence", "10")

        assert "error" in result


class TestCheckShippingGate(TestCase):
    def test_returns_none_when_no_session(self) -> None:
        ticket = Ticket.objects.create()
        assert _check_shipping_gate(ticket) is None

    def test_returns_none_when_gate_passes(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        assert _check_shipping_gate(ticket) is None

    def test_returns_structured_error_with_missing_phases(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(ticket=ticket)

        result = _check_shipping_gate(ticket)

        assert result is not None
        assert result["allowed"] is False
        assert "reviewing" in result["missing"]
        assert "testing" in result["missing"]
        assert "hint" in result


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
