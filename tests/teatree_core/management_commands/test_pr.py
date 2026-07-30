"""Tests for the pr management command."""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands.pr as pr_mod
from teatree.core.backend_protocols import UploadVerification
from teatree.core.models import Session, Ticket, Worktree
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


# ── PR commands ─────────────────────────────────────────────────────


def _shippable_ticket(*, repo: str = "/tmp/wt", branch: str = "feature-x") -> Ticket:
    """Build a ticket pre-advanced to REVIEWED with the shipping gate satisfied."""
    ticket = Ticket.objects.create(
        overlay="test",
        state=Ticket.State.REVIEWED,
        issue_url="https://example.com/issues/70",
    )
    session = Session.objects.create(overlay="test", ticket=ticket)
    for phase in ("testing", "reviewing", "retro"):
        session.visit_phase(phase)
    Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path=repo,
        branch=branch,
        extra={"worktree_path": repo},
    )
    return ticket


class TestPrCreate(TestCase):
    """``pr create`` is a thin wrapper around ``ticket.ship()`` (#140)."""

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_returns_error_when_no_worktree(self) -> None:
        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.REVIEWED)
        result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        assert "error" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_advances_to_shipped_when_gates_pass(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch.object(pr_mod, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_mod, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        # Default (async) path is queued with an explicit no-worker warning (#708).
        assert result["ticket_id"] == ticket.pk
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True
        assert "QUEUED, not performed" in result["warning"]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_validation_failure_keeps_state(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch.object(pr_mod, "_run_visual_qa_gate", return_value=None),
            patch.object(
                pr_mod,
                "validate_pr_metadata",
                return_value={"error": "MR validation failed", "details": ["Bad title"]},
            ),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED
        assert result["error"] == "MR validation failed"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_dry_run_returns_plan(self) -> None:
        ticket = _shippable_ticket()

        with (
            patch.object(pr_mod, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_mod, "validate_pr_metadata", return_value=None),
            patch.object(pr_mod.git, "last_commit_message", return_value=("Dry MR", "body")),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.pk), dry_run=True),
            )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED  # not advanced
        assert result["dry_run"] is True
        assert result["title"] == "Dry MR"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skip_validation_bypasses_check(self) -> None:
        ticket = _shippable_ticket()

        result = cast(
            "dict[str, object]",
            call_command("pr", "create", str(ticket.pk), skip_validation=True),
        )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "error" not in result


class TestPrCheckGates(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_without_session_returns_not_allowed(self) -> None:
        ticket = Ticket.objects.create(overlay="test")

        result = cast("dict[str, object]", call_command("pr", "check-gates", str(ticket.pk)))

        assert result["allowed"] is False

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_session_passes(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(overlay="test", ticket=ticket, agent_id="agent-1")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")

        result = cast("dict[str, object]", call_command("pr", "check-gates", str(ticket.pk), target_phase="shipping"))

        assert result["allowed"] is True

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_missing_phases_returns_not_allowed(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(overlay="test", ticket=ticket, agent_id="agent-1")
        # Only visited "testing", missing "reviewing" for shipping
        session.visit_phase("testing")

        result = cast("dict[str, object]", call_command("pr", "check-gates", str(ticket.pk), target_phase="shipping"))

        assert result["allowed"] is False
        assert "reviewing" in str(result["reason"])


class TestPrFetchIssue(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_without_tracker_returns_error(self) -> None:
        result = cast("dict[str, object]", call_command("pr", "fetch-issue", "https://example.com/issues/1"))

        assert "error" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_tracker(self) -> None:
        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = {"title": "Bug", "state": "opened", "description": "A bug"}

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_tracker):
            result = cast("dict[str, object]", call_command("pr", "fetch-issue", "https://example.com/issues/1"))

        assert result["title"] == "Bug"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_extracts_images_and_links(self) -> None:
        """fetch-issue extracts embedded images and external links from description."""
        desc = "See ![screenshot](/uploads/abc/img.png) and https://notion.so/page/12345 for context."
        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = {"title": "Task", "description": desc}

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_tracker):
            result = cast("dict[str, object]", call_command("pr", "fetch-issue", "https://example.com/issues/2"))

        assert result["_embedded_images"] == [{"alt": "screenshot", "path": "/uploads/abc/img.png"}]
        assert "https://notion.so/page/12345" in result["_external_links"]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_extracts_comment_images(self) -> None:
        """fetch-issue extracts images from comments/notes."""
        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = {
            "title": "Task",
            "description": "desc",
            "comments": [{"body": "See ![fix](/uploads/xyz/fix.png)"}],
        }

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_tracker):
            result = cast("dict[str, object]", call_command("pr", "fetch-issue", "https://example.com/issues/3"))

        comments = result["comments"]
        assert isinstance(comments, list)
        first = cast("dict[str, object]", comments[0])
        assert first["_embedded_images"] == [{"alt": "fix", "path": "/uploads/xyz/fix.png"}]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_non_dict_comments(self) -> None:
        """fetch-issue skips non-dict items in comments list."""
        mock_tracker = MagicMock()
        mock_tracker.get_issue.return_value = {
            "title": "Task",
            "description": "desc",
            "comments": ["not a dict", {"body": "valid"}],
        }

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_tracker):
            result = cast("dict[str, object]", call_command("pr", "fetch-issue", "https://example.com/issues/4"))

        assert "error" not in result


class TestPrDetectTenant(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_returns_overlay_variant(self) -> None:
        result = cast("str", call_command("pr", "detect-tenant"))

        assert result == "test_variant"


class TestPrPostTestPlan(TestCase):
    @pytest.fixture(autouse=True)
    def _no_on_behalf_gate(
        self,
        tmp_path_factory: pytest.TempPathFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Disable the on-behalf gate (#960) for transport-mechanics tests.

        ``post-test-plan`` is on-behalf-gated; the tests here exercise upload
        + body building, not the gate (its own suite lives in
        ``test_pr_post_test_plan_on_behalf_gate.py``).
        """
        from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate  # noqa: PLC0415

        disable_on_behalf_gate(tmp_path_factory, monkeypatch)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_without_code_host_returns_error(self) -> None:
        result = cast("dict[str, object]", call_command("pr", "post-test-plan", "100"))

        assert "error" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_with_code_host(self) -> None:
        mock_host = MagicMock()
        mock_host.post_pr_comment.return_value = {"id": 42}
        mock_host.list_pr_comments.return_value = []  # no existing note

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-test-plan",
                    "100",
                    repo="my/repo",
                    title="Evidence",
                    body="Test passed",
                ),
            )

        assert result == {"id": 42}
        call_kwargs = mock_host.post_pr_comment.call_args[1]
        assert call_kwargs["pr_iid"] == 100
        assert "## Evidence" in call_kwargs["body"]
        assert "Test passed" in call_kwargs["body"]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_updates_existing_note(self) -> None:
        """An existing note carrying THIS MR's marker is updated in place (F3.1).

        The match is now the hidden ``t3-e2e-evidence`` marker scoped to the MR
        target (``my/repo!100``) — not a naive ``"## Test Plan" in body`` scan —
        so a colleague's unrelated ``## Test Plan`` comment can never be clobbered.
        """
        mock_host = MagicMock()
        mock_host.list_pr_comments.return_value = [
            {
                "id": 999,
                "body": "## Test Plan\n\nOld content\n\n<!-- t3-e2e-evidence ticket=my/repo!100 -->",
                "system": False,
            },
        ]
        mock_host.update_pr_comment.return_value = {"id": 999}

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-test-plan",
                    "100",
                    repo="my/repo",
                    body="Updated content",
                ),
            )

        assert result == {"id": 999}
        mock_host.update_pr_comment.assert_called_once()
        call_kwargs = mock_host.update_pr_comment.call_args[1]
        assert call_kwargs["comment_id"] == 999
        assert "Updated content" in call_kwargs["body"]
        mock_host.post_pr_comment.assert_not_called()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_does_not_clobber_a_colleagues_unmarked_test_plan_comment(self) -> None:
        """A ``## Test Plan`` comment WITHOUT our marker is left alone (F3.1).

        The former naive ``"## Test Plan" in body`` match would have updated a
        colleague's comment; the marker-scoped match now creates a fresh note.
        """
        mock_host = MagicMock()
        mock_host.list_pr_comments.return_value = [
            {"id": 999, "body": "## Test Plan\n\nSomeone else's note", "system": False},
        ]
        mock_host.post_pr_comment.return_value = {"id": 1000}

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command("pr", "post-test-plan", "100", repo="my/repo", body="Mine"),
            )

        assert result == {"id": 1000}
        mock_host.post_pr_comment.assert_called_once()
        mock_host.update_pr_comment.assert_not_called()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_uploads_files(self) -> None:
        """Each uploaded file passes the #2156 verify_upload gate before it is embedded (F3.1)."""
        mock_host = MagicMock()
        mock_host.upload_file.return_value = {"markdown": "![screenshot](/uploads/abc/img.png)"}
        mock_host.verify_upload.return_value = UploadVerification(ok=True, embed_url="/uploads/abc/img.png")
        mock_host.list_pr_comments.return_value = []
        mock_host.post_pr_comment.return_value = {"id": 55}

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-test-plan",
                    "100",
                    repo="my/repo",
                    body="Evidence",
                    files=["/tmp/img.png"],
                ),
            )

        mock_host.upload_file.assert_called_once_with(repo="my/repo", filepath="/tmp/img.png")
        mock_host.verify_upload.assert_called_once()
        # The embed is the VERIFIED relative reference (not the blind upload["markdown"]);
        # the label is the artifact's filename.
        body = mock_host.post_pr_comment.call_args[1]["body"]
        assert "![img.png](/uploads/abc/img.png)" in body

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_unverifiable_upload_refuses_the_post(self) -> None:
        """A failed verify_upload (#2156) refuses the post — no note references a broken upload (F3.1)."""
        mock_host = MagicMock()
        mock_host.upload_file.return_value = {"markdown": "![bad](/uploads/xyz/bad.png)"}
        mock_host.verify_upload.return_value = UploadVerification(ok=False, embed_url="", detail="HTTP 404")
        mock_host.list_pr_comments.return_value = []

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            result = cast(
                "dict[str, object]",
                call_command("pr", "post-test-plan", "100", repo="my/repo", body="x", files=["/tmp/bad.png"]),
            )

        assert "error" in result
        assert "upload check" in str(result["error"])
        # No note posted or updated when an embed cannot be verified.
        mock_host.post_pr_comment.assert_not_called()
        mock_host.update_pr_comment.assert_not_called()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_without_body(self) -> None:
        mock_host = MagicMock()
        mock_host.post_pr_comment.return_value = {"id": 43}
        mock_host.list_pr_comments.return_value = []

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            cast(
                "dict[str, object]",
                call_command(
                    "pr",
                    "post-test-plan",
                    "101",
                    title="Screenshot",
                ),
            )

        call_kwargs = mock_host.post_pr_comment.call_args[1]
        assert "_No details provided._" in call_kwargs["body"]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_uses_overlay_ci_project_path(self) -> None:
        """When no repo is given, falls back to overlay.metadata.get_ci_project_path()."""
        mock_host = MagicMock()
        mock_host.post_pr_comment.return_value = {"id": 44}
        mock_host.list_pr_comments.return_value = []

        with patch.object(pr_mod, "code_host_from_overlay", return_value=mock_host):
            call_command("pr", "post-test-plan", "102", title="T")

        call_kwargs = mock_host.post_pr_comment.call_args[1]
        assert call_kwargs["repo"] == "test/project"
