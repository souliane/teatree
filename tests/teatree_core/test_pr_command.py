from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree import visual_qa
from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands.pr import (
    _check_shipping_gate,
    _mr_auto_labels,
    _resolve_base_url,
    _run_visual_qa_gate,
    _sanitize_close_keywords,
)
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
        host.current_user.return_value = "souliane"
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
        assert spec.assignee == "souliane"

    def test_assignee_falls_back_to_git_user_name_when_host_returns_empty(self) -> None:
        host = MagicMock()
        host.create_pr.return_value = {"iid": 13}
        host.current_user.return_value = ""
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/56")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/backend", branch="feature-branch")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.management.commands.pr._last_commit_message", return_value=("", "")),
        ):
            call_command("pr", "create", str(ticket.id), "--title", "feat: fallback")

        (spec,) = host.create_pr.call_args.args
        assert spec.assignee == "dev"  # from patched git.config_value in setUp

    def test_assignee_propagates_when_host_current_user_raises(self) -> None:
        """Host lookup errors surface to the caller — fallback is for empty, not broken."""
        host = MagicMock()
        host.create_pr.return_value = {"iid": 14}
        host.current_user.side_effect = RuntimeError("gh not authenticated")
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/57")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/backend", branch="feature-branch")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.management.commands.pr._last_commit_message", return_value=("", "")),
            pytest.raises(RuntimeError, match="gh not authenticated"),
        ):
            call_command("pr", "create", str(ticket.id), "--title", "feat: resilient")

        host.create_pr.assert_not_called()


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


class TestResolveBaseUrl(TestCase):
    def test_returns_default_when_worktree_is_none(self) -> None:
        assert _resolve_base_url(None) == "http://127.0.0.1:8000"

    def test_prefers_frontend_url(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/wt",
            branch="feat",
            extra={"urls": {"frontend": "http://localhost:4201", "backend": "http://localhost:8001"}},
        )
        assert _resolve_base_url(worktree) == "http://localhost:4201"

    def test_falls_back_to_backend(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="/tmp/wt",
            branch="feat",
            extra={"urls": {"backend": "http://localhost:8001"}},
        )
        assert _resolve_base_url(worktree) == "http://localhost:8001"

    def test_falls_back_to_localhost_when_no_urls(self) -> None:
        ticket = Ticket.objects.create()
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/wt", branch="feat")
        assert _resolve_base_url(worktree) == "http://127.0.0.1:8000"


class TestRunVisualQAGate(TestCase):
    def _ticket(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/77")
        Worktree.objects.create(ticket=ticket, overlay="test", repo_path="/tmp/wt", branch="feat-x")
        return ticket

    def test_skipped_run_does_not_pollute_extra(self) -> None:
        ticket = self._ticket()
        clean = visual_qa.VisualQAReport(targets=[], skipped_reason="no frontend changes")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=clean),
        ):
            assert _run_visual_qa_gate(ticket) is None

        ticket.refresh_from_db()
        assert "visual_qa" not in ticket.extra

    def test_records_summary_when_pages_checked(self) -> None:
        ticket = self._ticket()
        page = visual_qa.PageResult(url="http://x/", screenshot_path=".t3/visual_qa/00-root.png")
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            assert _run_visual_qa_gate(ticket) is None

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["pages_checked"] == 1
        assert ticket.extra["visual_qa"]["errors"] == 0

    def test_returns_error_when_findings(self) -> None:
        ticket = self._ticket()
        page = visual_qa.PageResult(
            url="http://x/",
            errors=[visual_qa.PageError(url="http://x/", kind="page", message="boom")],
        )
        report = visual_qa.VisualQAReport(targets=["/"], pages=[page], base_url="http://x")
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", return_value=report),
        ):
            result = _run_visual_qa_gate(ticket)

        assert result is not None
        assert result["allowed"] is False
        assert "1 blocking finding" in result["error"]
        assert "## Visual QA" in result["report_markdown"]

        ticket.refresh_from_db()
        assert ticket.extra["visual_qa"]["errors"] == 1

    def test_skip_reason_propagates(self) -> None:
        ticket = self._ticket()
        captured: dict[str, str] = {}

        def fake_evaluate(**kwargs: object) -> visual_qa.VisualQAReport:
            captured["skip_reason"] = str(kwargs.get("skip_reason", ""))
            return visual_qa.VisualQAReport(targets=[], skipped_reason="--skip: my reason")

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(visual_qa, "evaluate", side_effect=fake_evaluate),
        ):
            assert _run_visual_qa_gate(ticket, skip_reason="my reason") is None

        assert captured["skip_reason"] == "my reason"
