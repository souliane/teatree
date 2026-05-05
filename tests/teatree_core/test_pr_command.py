from collections.abc import Iterator
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree import visual_qa
from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands.pr import (
    _check_shipping_gate,
    _resolve_base_url,
    _run_visual_qa_gate,
    _slug_from_remote,
)
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.orphan_guard import BranchReport, BranchStatus
from teatree.core.overlay_loader import reset_overlay_cache
from tests.teatree_core.conftest import CommandOverlay


@pytest.fixture(autouse=True)
def clear_overlay_cache() -> Iterator[None]:
    reset_overlay_cache()
    yield
    reset_overlay_cache()


_MOCK_OVERLAY = {"test": CommandOverlay()}


def _shippable_ticket() -> Ticket:
    """Build a ticket pre-advanced to REVIEWED with the shipping gate satisfied."""
    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
    session = Session.objects.create(ticket=ticket, overlay="test")
    session.visit_phase("testing")
    session.visit_phase("reviewing")
    session.visit_phase("retro")
    Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path="/tmp/backend",
        branch="feature-branch",
        extra={"worktree_path": "/tmp/backend"},
    )
    return ticket


class TestPrCreateThinWrapper(TestCase):
    """``pr create`` validates gates then triggers ``ticket.ship()`` (#140)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_advances_to_shipped_when_gates_pass(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "_validate_mr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result == {"ticket_id": ticket.pk, "state": Ticket.State.SHIPPED}

    def test_returns_error_when_no_worktree(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))
        assert "error" in result

    def test_dry_run_returns_preview_without_transition(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "_validate_mr_metadata", return_value=None),
            patch.object(pr_command.git, "last_commit_message", return_value=("feat: x", "body")),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id), dry_run=True))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # unchanged
        assert result["dry_run"] is True
        assert result["title"] == "feat: x"
        assert result["branch"] == "feature-branch"

    def test_resolves_ticket_by_issue_url(self) -> None:
        # Calling `pr create` with the issue URL (or trailing issue number)
        # resolves to the ticket by issue_url so users don't have to look up
        # the internal DB pk first.
        ticket = _shippable_ticket()
        ticket.issue_url = "https://github.com/souliane/teatree/issues/466"
        ticket.save(update_fields=["issue_url"])

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "_validate_mr_metadata", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", "https://github.com/souliane/teatree/issues/466"),
            )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result["ticket_id"] == ticket.pk

    def test_blocked_when_visual_qa_fails(self) -> None:
        ticket = _shippable_ticket()
        failure = pr_command.VisualQAGateFailure(
            allowed=False,
            error="Visual QA found 1 blocking finding(s).",
            visual_qa={},
            report_markdown="## Visual QA",
            hint="fix it",
        )

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=failure),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.id)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # not advanced
        assert result["allowed"] is False


class TestSlugFromRemote(TestCase):
    def test_github_ssh(self) -> None:
        assert _slug_from_remote("git@github.com:souliane/teatree.git") == "souliane/teatree"

    def test_github_https(self) -> None:
        assert _slug_from_remote("https://github.com/souliane/teatree.git") == "souliane/teatree"

    def test_gitlab_nested_namespace(self) -> None:
        assert _slug_from_remote("git@gitlab.com:acme/team/backend.git") == "acme/team/backend"

    def test_no_dot_git_suffix(self) -> None:
        assert _slug_from_remote("https://github.com/souliane/teatree") == "souliane/teatree"

    def test_empty_returns_empty(self) -> None:
        assert _slug_from_remote("") == ""


class TestEnsurePr(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_no_op_when_branch_has_open_pr(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-x"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-x",
                    status=BranchStatus.OPEN_PR,
                    ahead_count=3,
                    open_pr_url="https://gitlab.com/org/repo/-/merge_requests/42",
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert result["skipped"] == "open PR exists"
        assert "42" in str(result["url"])
        host.create_pr.assert_not_called()

    def test_no_op_when_branch_synced(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-y"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-y",
                    status=BranchStatus.SYNCED,
                    ahead_count=0,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert "synced" in str(result["skipped"])
        host.create_pr.assert_not_called()

    def test_defers_when_branch_not_on_remote(self) -> None:
        host = MagicMock()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-z"),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-z",
                    status=BranchStatus.UNPUSHED_ORPHAN,
                    ahead_count=2,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert "not on remote yet" in str(result["skipped"])
        assert "feat-z" in str(result["hint"])
        host.create_pr.assert_not_called()

    def test_creates_pr_when_pushed_orphan(self) -> None:
        host = MagicMock()
        host.create_pr.return_value = {"url": "https://github.com/souliane/teatree/pull/999"}
        host.current_user.return_value = "souliane"
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch.object(pr_command.git, "current_branch", return_value="feat-q"),
            patch.object(pr_command.git, "remote_url", return_value="git@github.com:souliane/teatree.git"),
            patch.object(pr_command.git, "last_commit_message", return_value=("feat: cool thing", "body")),
            patch.object(
                pr_command,
                "classify_branch",
                return_value=BranchReport(
                    repo=".",
                    branch="feat-q",
                    status=BranchStatus.PUSHED_ORPHAN,
                    ahead_count=5,
                ),
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "ensure-pr"))

        assert result["url"] == "https://github.com/souliane/teatree/pull/999"
        assert result["branch"] == "feat-q"
        (spec,) = host.create_pr.call_args.args
        assert spec.draft is False
        assert spec.branch == "feat-q"
        assert spec.repo == "souliane/teatree"
        assert spec.title == "feat: cool thing"


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


class TestSweep(TestCase):
    """``pr sweep`` lists all of the user's open PRs across the forge (#466)."""

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_returns_open_prs_for_authenticated_user(self) -> None:
        from teatree.core.overlay import OverlayConfig  # noqa: PLC0415

        host = MagicMock()
        host.list_my_open_prs.return_value = [
            {
                "iid": 1,
                "title": "feat: x",
                "web_url": "https://gitlab.com/org/repo/-/merge_requests/1",
                "source_branch": "feat-x",
                "target_branch": "main",
            },
            {
                "iid": 2,
                "title": "fix: y",
                "web_url": "https://gitlab.com/org/other/-/merge_requests/2",
                "source_branch": "fix-y",
                "target_branch": "develop",
            },
        ]
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)
        overlay = CommandOverlay()
        # Per-instance config so we don't mutate the class-level default shared by other tests.
        overlay.config = OverlayConfig()
        overlay.config.get_gitlab_username = lambda: "adrien"  # type: ignore[method-assign]

        with patch("teatree.core.overlay_loader._discover_overlays", return_value={"test": overlay}):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert result["author"] == "adrien"
        assert result["count"] == 2
        prs = cast("list[dict[str, object]]", result["prs"])
        assert prs[0]["target_branch"] == "main"
        assert prs[1]["target_branch"] == "develop"
        host.list_my_open_prs.assert_called_once_with("adrien")

    def test_falls_back_to_current_user_when_no_username_configured(self) -> None:
        host = MagicMock()
        host.current_user.return_value = "souliane"
        host.list_my_open_prs.return_value = []
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert result["author"] == "souliane"
        assert result["count"] == 0
        host.list_my_open_prs.assert_called_once_with("souliane")

    def test_returns_error_when_no_code_host_configured(self) -> None:
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: None)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert "error" in result

    def test_returns_error_when_username_unresolved(self) -> None:
        host = MagicMock()
        host.current_user.return_value = ""
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)

        with patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY):
            result = cast("dict[str, object]", call_command("pr", "sweep"))

        assert "error" in result
        host.list_my_open_prs.assert_not_called()


class TestCheckShippingGate(TestCase):
    def test_returns_none_when_no_session(self) -> None:
        ticket = Ticket.objects.create()
        assert _check_shipping_gate(ticket) is None

    def test_returns_none_when_gate_passes(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
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
