"""The CLI forge writers route through the shared #117 scrub seam (U14).

``t3 ticket comment`` wrote to the forge with no public-repo leak scrub / #117
audit — laxer than the MCP surface. It now routes its body through
:func:`teatree.core.send_proxy.route_forge_write`, so a SendAudit row is written
before the backend call. The MR/PR test-plan poster's own scrub is pinned by
``tests/teatree_core/pr_command/test_post_test_plan_leak_scan.py``.

Since #162 the command has TWO write paths — a normative purpose edits the
description, an observational one posts a comment — so both are pinned here.
The description path is the one worth stating explicitly: moving a requirement
out of a comment and into the body must not move it out of the leak gate.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from teatree.core.models import ConfigSetting, SendAudit
from teatree.core.send_proxy import OutboundLeakError
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_ISSUE_URL = "https://gitlab.com/org/repo/-/work_items/469"
# The audited destination is the repo SLUG, not the issue URL: it is what the
# privacy gate's public/private lookup keys on, and what the MCP twin already sent.
_REPO = "org/repo"


@pytest.fixture(autouse=True)
def _allow_expected_repo() -> None:
    ConfigSetting.objects.set_value("send_proxy_allowlist", [f"gitlab:{_REPO}"])


def _owner_host() -> MagicMock:
    host = MagicMock()
    host.get_issue.return_value = {
        "web_url": _ISSUE_URL,
        "description": "Original body.",
        "author": {"username": "adrien.cossa"},
    }
    host.current_user.return_value = "adrien.cossa"
    host.repo_for_issue_url.return_value = "org/repo"
    host.post_issue_comment.return_value = {"id": 4242}

    def _update(*, issue_url: str, body: str) -> dict[str, object]:
        host.get_issue.return_value = {**host.get_issue.return_value, "description": body}
        return {"web_url": issue_url}

    host.update_issue.side_effect = _update
    return host


class TicketCommentRoutesThroughSeam(TestCase):
    def test_a_status_comment_writes_a_send_audit_row(self) -> None:
        host = _owner_host()
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
        ):
            call_command("ticket", "comment", _ISSUE_URL, purpose="status", body="A clarifying question")
        assert SendAudit.objects.filter(destination=f"gitlab:{_REPO}", action="issue_note_status").exists()

    def test_a_leaking_comment_is_refused_before_the_backend(self) -> None:
        host = _owner_host()
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["SECRETCORP"], [])),
            pytest.raises(OutboundLeakError, match="privacy gate refused"),
        ):
            call_command("ticket", "comment", _ISSUE_URL, purpose="status", body="ship for SECRETCORP")
        host.post_issue_comment.assert_not_called()

    def test_a_description_append_writes_a_send_audit_row(self) -> None:
        host = _owner_host()
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
        ):
            call_command("ticket", "comment", _ISSUE_URL, purpose="requirement", body="also handle the empty case")
        assert SendAudit.objects.filter(destination=f"gitlab:{_REPO}", action="issue_note_requirement").exists()

    def test_a_leaking_requirement_is_refused_before_the_description_write(self) -> None:
        host = _owner_host()
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["SECRETCORP"], [])),
            pytest.raises(OutboundLeakError, match="privacy gate refused"),
        ):
            call_command("ticket", "comment", _ISSUE_URL, purpose="requirement", body="ship for SECRETCORP")
        host.update_issue.assert_not_called()


class TicketCreateSubRoutesThroughSeam(TestCase):
    """``t3 ticket create-sub`` scrubs the child title/body/labels like its sibling `comment`."""

    def test_create_sub_writes_a_send_audit_row(self) -> None:
        host = MagicMock()
        host.create_sub_issue.return_value = {"iid": 7, "web_url": f"{_ISSUE_URL}/7"}
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
        ):
            call_command("ticket", "create-sub", parent=_ISSUE_URL, title="Child task")
        assert SendAudit.objects.filter(destination=f"gitlab:{_REPO}", action="ticket_create_sub").exists()
        host.create_sub_issue.assert_called_once()

    def test_a_leaking_title_is_refused_before_the_backend(self) -> None:
        host = MagicMock()
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["SECRETCORP"], [])),
            pytest.raises(OutboundLeakError, match="privacy gate refused"),
        ):
            call_command("ticket", "create-sub", parent=_ISSUE_URL, title="ship for SECRETCORP")
        host.create_sub_issue.assert_not_called()

    def test_a_leaking_label_is_refused_before_the_backend(self) -> None:
        host = MagicMock()
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["SECRETCORP"], [])),
            pytest.raises(OutboundLeakError, match="privacy gate refused"),
        ):
            call_command("ticket", "create-sub", parent=_ISSUE_URL, title="Child", labels="SECRETCORP,ok")
        host.create_sub_issue.assert_not_called()
