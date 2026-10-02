"""`t3 ticket comment` — post a comment to an issue/work-item from the CLI."""

from typing import cast
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_ISSUE_URL = "https://gitlab.com/org/repo/-/work_items/469"


class TicketCommentCommandTest(TestCase):
    def test_posts_body_via_resolved_code_host(self) -> None:
        host = MagicMock()
        host.post_issue_comment.return_value = {"id": 4242}

        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "comment", _ISSUE_URL, body="A clarifying question"),
            )

        assert result == {"issue_url": _ISSUE_URL, "comment_id": 4242}
        host.post_issue_comment.assert_called_once_with(
            issue_url=_ISSUE_URL,
            body="A clarifying question",
        )


class TicketCommentBodyFileTest(TestCase):
    def test_reads_body_from_file(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        host = MagicMock()
        host.post_issue_comment.return_value = {"id": 1}

        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "comment.md"
            body_path.write_text("From a file\n", encoding="utf-8")

            with (
                patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
                patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            ):
                call_command("ticket", "comment", _ISSUE_URL, body_file=str(body_path))

        host.post_issue_comment.assert_called_once_with(
            issue_url=_ISSUE_URL,
            body="From a file\n",
        )


class TicketCommentErrorTest(TestCase):
    def test_errors_when_no_body_supplied(self) -> None:
        with patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "comment", _ISSUE_URL),
            )
        assert result == {"error": "No comment body: pass --body or --body-file"}

    def test_errors_when_no_code_host_resolves(self) -> None:
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "comment", _ISSUE_URL, body="hi"),
            )
        assert result == {"error": f"No code host could be resolved for {_ISSUE_URL}"}

    def test_propagates_code_host_error(self) -> None:
        host = MagicMock()
        host.post_issue_comment.return_value = {"error": "Could not resolve project: org/repo"}
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        ):
            result = cast(
                "dict[str, object]",
                call_command("ticket", "comment", _ISSUE_URL, body="hi"),
            )
        assert result == {"error": "Could not resolve project: org/repo"}
