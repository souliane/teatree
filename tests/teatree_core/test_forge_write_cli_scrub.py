"""The CLI forge writers route through the shared #117 scrub seam (U14).

``t3 ticket comment`` and the test-plan note poster wrote to the forge with no
public-repo leak scrub / #117 audit — laxer than the MCP surface. Both now route
their body through :func:`teatree.core.send_proxy.route_forge_write`, so a
SendAudit row is written before the backend call.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.backends import loader as loader_mod
from teatree.core import overlay_loader as overlay_loader_mod
from teatree.core.management.commands._test_plan import post as post_mod
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
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["SECRETCORP"], [])),
            pytest.raises(OutboundLeakError, match="privacy gate refused"),
        ):
            call_command("ticket", "comment", _ISSUE_URL, body="ship for SECRETCORP")
        host.post_issue_comment.assert_not_called()


class TestPlanNoteRoutesThroughSeam(TestCase):
    def test_body_file_note_writes_a_send_audit_row(self) -> None:
        host = MagicMock()
        host.list_issue_comments.return_value = []
        host.post_issue_comment.return_value = {"id": 1, "web_url": "https://gitlab.com/org/repo/-/work_items/469#n1"}
        with (
            patch.object(post_mod, "on_behalf_block_message", return_value=""),
            patch.object(
                post_mod, "require_on_behalf_approval", side_effect=lambda *, target, action, publish: publish()
            ),
            patch.object(post_mod, "notify_user_on_behalf_post"),
            patch.object(post_mod, "check_blocked_body_from_config"),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
        ):
            post_mod.post_body_file_comment(host, issue_url=_ISSUE_URL, ticket_id="T-1", body="clean note body")
        assert SendAudit.objects.filter(destination=_ISSUE_URL, action="post_e2e_evidence").exists()
