"""`t3 ticket comment` — record a note on an issue where its `--purpose` belongs (#162).

The command used to post whatever it was given as a comment. A lane reads the
description and never the comments, so that silently dropped every requirement
it carried. These tests pin the routing (normative → description, observational
→ comment), the refusals (no purpose, external author), and the pre-existing
behaviour the rewrite had to keep: `--body-file`, the empty-body guard, the
no-host guard, and forge-error propagation.
"""

import io
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from teatree.core.models import ConfigSetting
from tests.teatree_core.conftest import CommandOverlay

pytestmark = pytest.mark.usefixtures("configured_banned_term_registry")

_MOCK_OVERLAY = {"test": CommandOverlay()}
_ISSUE_URL = "https://gitlab.com/org/repo/-/work_items/469"
_OWNER = "adrien.cossa"


@pytest.fixture(autouse=True)
def _allow_expected_issue_repo() -> None:
    ConfigSetting.objects.set_value("send_proxy_allowlist", ["gitlab:org/repo"])


def _owner_host(*, author: str = _OWNER, description: str = "Original body.") -> MagicMock:
    host = MagicMock()
    host.get_issue.return_value = {
        "web_url": _ISSUE_URL,
        "description": description,
        "author": {"username": author},
        "updated_at": "2026-09-22T00:00:00Z",
    }
    host.current_user.return_value = _OWNER
    host.repo_for_issue_url.return_value = "org/repo"
    host.post_issue_comment.return_value = {"id": 4242}

    def _update(*, issue_url: str, body: str) -> dict[str, object]:
        host.get_issue.return_value = {**host.get_issue.return_value, "description": body}
        return {"web_url": issue_url}

    host.update_issue.side_effect = _update
    return host


def _run(host: MagicMock | None, **kwargs: object) -> dict[str, object]:
    with (
        patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
        patch.object(loader_mod, "get_code_host_for_url", return_value=host),
    ):
        return cast("dict[str, object]", call_command("ticket", "comment", _ISSUE_URL, **kwargs))


def _refused(host: MagicMock | None, **kwargs: object) -> str:
    err = io.StringIO()
    with (
        patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
        patch.object(loader_mod, "get_code_host_for_url", return_value=host),
        pytest.raises(SystemExit) as caught,
    ):
        call_command("ticket", "comment", _ISSUE_URL, stderr=err, **kwargs)
    assert caught.value.code == 1
    return err.getvalue()


class TicketCommentPurposeRoutingTest(TestCase):
    def test_a_requirement_appends_to_the_description_and_posts_no_comment(self) -> None:
        host = _owner_host()
        result = _run(host, purpose="requirement", body="also handle the empty case")

        assert result["outcome"] == "appended"
        assert result["purpose"] == "requirement"
        host.post_issue_comment.assert_not_called()
        written = host.update_issue.call_args.kwargs["body"]
        assert written.startswith("Original body.")
        assert "also handle the empty case" in written

    def test_a_status_note_still_posts_a_comment(self) -> None:
        host = _owner_host()
        result = _run(host, purpose="status", body="A clarifying question")

        assert result == {
            "issue_url": _ISSUE_URL,
            "outcome": "commented",
            "purpose": "status",
            "comment_id": 4242,
        }
        host.post_issue_comment.assert_called_once_with(issue_url=_ISSUE_URL, body="A clarifying question")
        host.update_issue.assert_not_called()

    def test_a_sweep_run_refuses_even_an_observational_comment(self) -> None:
        host = _owner_host()
        refusal = _refused(host, purpose="evidence", body="a screenshot", sweep_run_id="run-1")

        assert "sweep posts no comments" in refusal
        host.post_issue_comment.assert_not_called()

    def test_an_externally_authored_ticket_is_refused_entirely(self) -> None:
        host = _owner_host(author="someone.else")
        refusal = _refused(host, purpose="requirement", body="do X")

        assert "refusing to modify" in refusal
        host.update_issue.assert_not_called()
        host.post_issue_comment.assert_not_called()


class TicketCommentOurTicketsOnlyTest(TestCase):
    def test_a_status_comment_on_a_colleagues_ticket_is_refused(self) -> None:
        host = _owner_host(author="someone.else")
        refusal = _refused(host, purpose="status", body="a note")

        assert "refusing to modify" in refusal
        host.post_issue_comment.assert_not_called()


class TicketCommentRefusalTest(TestCase):
    def test_an_untyped_note_is_refused_and_names_the_purposes(self) -> None:
        host = _owner_host()
        refusal = _refused(host, body="something")

        assert "explicit purpose" in refusal
        assert "requirement" in refusal
        host.post_issue_comment.assert_not_called()
        host.update_issue.assert_not_called()

    def test_an_unknown_purpose_is_refused(self) -> None:
        host = _owner_host()
        refusal = _refused(host, purpose="urgent", body="something")

        assert "explicit purpose" in refusal
        host.post_issue_comment.assert_not_called()


class TicketCommentBodyFileTest(TestCase):
    def test_reads_body_from_file(self) -> None:
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        host = _owner_host()
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "comment.md"
            body_path.write_text("From a file\n", encoding="utf-8")
            _run(host, purpose="status", body_file=str(body_path))

        host.post_issue_comment.assert_called_once_with(issue_url=_ISSUE_URL, body="From a file\n")


class TicketCommentErrorTest(TestCase):
    def test_errors_when_no_body_supplied(self) -> None:
        assert "No note body: pass --body or --body-file" in _refused(_owner_host(), purpose="status")

    def test_errors_when_no_code_host_resolves(self) -> None:
        assert f"No code host could be resolved for {_ISSUE_URL}" in _refused(None, purpose="status", body="hi")

    def test_propagates_code_host_error(self) -> None:
        host = _owner_host()
        host.post_issue_comment.return_value = {"error": "Could not resolve project: org/repo"}
        assert "Could not resolve project: org/repo" in _refused(host, purpose="status", body="hi")

    def test_a_failed_description_update_is_reported_not_swallowed(self) -> None:
        host = _owner_host()
        host.update_issue.side_effect = None
        host.update_issue.return_value = {"error": "Could not resolve project: org/repo"}
        assert "Could not resolve project: org/repo" in _refused(host, purpose="requirement", body="do X")
