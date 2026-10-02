"""The CLI forge writers route through the shared #117 scrub seam (U14).

``t3 ticket comment`` wrote to the forge with no public-repo leak scrub / #117
audit — laxer than the MCP surface. It now routes its body through
:func:`teatree.core.send_proxy.route_forge_write`, so a SendAudit row is written
before the backend call. The MR/PR test-plan poster's own scrub is pinned by
``tests/teatree_core/pr_command/test_post_test_plan_leak_scan.py``.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from teatree.core.models import SendAudit
from teatree.core.send_proxy import OutboundLeakError
from tests.teatree_core.conftest import CommandOverlay

_MOCK_OVERLAY = {"test": CommandOverlay()}
_ISSUE_URL = "https://gitlab.com/org/repo/-/work_items/469"


class TicketCommentRoutesThroughSeam(TestCase):
    def test_comment_writes_a_send_audit_row(self) -> None:
        host = MagicMock()
        host.post_issue_comment.return_value = {"id": 4242}
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
        ):
            call_command("ticket", "comment", _ISSUE_URL, body="A clarifying question")
        assert SendAudit.objects.filter(destination=_ISSUE_URL, action="ticket_comment").exists()

    def test_a_leaking_comment_is_refused_before_the_backend(self) -> None:
        host = MagicMock()
        with (
            patch.object(overlay_loader_mod, "get_all_overlays", return_value=_MOCK_OVERLAY),
            patch.object(loader_mod, "get_code_host_for_url", return_value=host),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["SECRETCORP"], [])),
            pytest.raises(OutboundLeakError, match="privacy gate refused"),
        ):
            call_command("ticket", "comment", _ISSUE_URL, body="ship for SECRETCORP")
        host.post_issue_comment.assert_not_called()


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
        assert SendAudit.objects.filter(destination=_ISSUE_URL, action="ticket_create_sub").exists()
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
