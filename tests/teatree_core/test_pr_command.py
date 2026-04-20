from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands.pr import _check_shipping_gate, _mr_auto_labels, _sanitize_close_keywords
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
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch.object(pr_command.git, "config_value", return_value="dev"))
        self.enterContext(
            patch.object(pr_command.git, "last_commit_message", return_value=("commit subject", "commit body")),
        )

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_reads_auto_labels_from_overlay(self) -> None:
        host = MagicMock()
        host.create_pr.return_value = {"iid": 12}
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/55")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/backend", branch="feature-branch")

        # CommandOverlay.config.mr_auto_labels returns [] (default), so labels=[]
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.management.commands.pr._last_commit_message", return_value=("", "")),
        ):
            result = call_command("pr", "create", str(ticket.id), "--title", "feat: add labels")

        assert result == {"iid": 12}
        (spec,) = host.create_pr.call_args.args
        assert spec.repo == "/tmp/backend"
        assert spec.branch == "feature-branch"
        assert spec.title == "feat: add labels"
        assert spec.assignee == "dev"


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


class TestSanitizeCloseKeywords:
    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            # Same-project short refs
            ("Closes #123", "Relates to #123"),
            ("Fixes #42", "Relates to #42"),
            ("Resolves #7", "Relates to #7"),
            ("closes #123", "Relates to #123"),
            ("fixes #42", "Relates to #42"),
            ("resolves #7", "Relates to #7"),
            ("See Closes #1 and Fixes #2", "See Relates to #1 and Relates to #2"),
            # Cross-project short refs
            ("Closes group/project#99", "Relates to group/project#99"),
            ("Fixes org/sub/repo#5", "Relates to org/sub/repo#5"),
            # Full URL refs
            (
                "Closes https://gitlab.com/org/project/-/issues/729",
                "Relates to https://gitlab.com/org/project/-/issues/729",
            ),
            (
                "Fixes https://gitlab.com/org/sub/repo/-/issues/42",
                "Relates to https://gitlab.com/org/sub/repo/-/issues/42",
            ),
            (
                "Resolves https://github.com/owner/repo/issues/10",
                "Relates to https://github.com/owner/repo/issues/10",
            ),
            # Mixed refs in one description
            (
                "Closes #1\nFixes https://gitlab.com/g/p/-/issues/2\nResolves g/p#3",
                "Relates to #1\nRelates to https://gitlab.com/g/p/-/issues/2\nRelates to g/p#3",
            ),
            # No ticket ref
            ("No ticket ref here", "No ticket ref here"),
            ("", ""),
        ],
    )
    def test_replaces_close_keywords_when_close_ticket_false(self, description: str, expected: str) -> None:
        assert _sanitize_close_keywords(description, close_ticket=False) == expected

    def test_leaves_description_unchanged_when_close_ticket_true(self) -> None:
        assert _sanitize_close_keywords("Closes #123", close_ticket=True) == "Closes #123"


class TestMrAutoLabels:
    def test_default_overlay_returns_empty(self) -> None:
        """CommandOverlay has no auto labels, so _mr_auto_labels returns []."""
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = _mr_auto_labels()
            assert result == []

    def test_overlay_with_string_labels(self) -> None:
        """When overlay returns a comma-separated string, it's split."""
        mock_overlay = MagicMock()
        mock_overlay.config.mr_auto_labels = "label-a, label-b"
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": mock_overlay},
        ):
            result = _mr_auto_labels()
            assert result == ["label-a", "label-b"]

    def test_non_iterable_returns_empty(self) -> None:
        """_mr_auto_labels returns [] for non-iterable value."""
        mock_overlay = MagicMock()
        mock_overlay.config.mr_auto_labels = 42
        with patch(
            "teatree.core.overlay_loader._discover_overlays",
            return_value={"test": mock_overlay},
        ):
            result = _mr_auto_labels()
            assert result == []
