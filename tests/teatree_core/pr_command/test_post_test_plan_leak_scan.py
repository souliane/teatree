"""``pr post-test-plan`` routes its body through the scanned forge-write seam (CC-1).

``post-test-plan`` (and its deprecated ``post-evidence`` alias) built the PR
comment body and posted it via ``host.post_pr_comment`` / ``update_pr_comment``
with NO public-repo leak scan — laxer than the MCP / ticket / test-plan-body
writers. It now routes the body through
:func:`teatree.core.send_proxy.route_forge_write` BEFORE consuming the on-behalf
approval, so a body carrying a customer codename bound for a public forge is
REFUSED (no upload side effect on the comment, nothing posted raw) and every
post is audited.
"""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from teatree.core.models import SendAudit
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate

from ._shared import _MOCK_OVERLAY


class TestPostTestPlanLeakScan(TestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
        self._monkeypatch = monkeypatch
        disable_on_behalf_gate(tmp_path_factory, monkeypatch)

    def _host(self) -> MagicMock:
        host = MagicMock()
        host.list_pr_comments.return_value = []
        host.post_pr_comment.return_value = {"id": 1}
        return host

    def test_leaking_body_to_a_public_repo_is_refused_and_never_posted(self) -> None:
        host = self._host()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            result = cast(
                "dict[str, object]", call_command("pr", "post-test-plan", "10", "--body", "rolling out for Contoso")
            )
        assert "error" in result
        assert "privacy gate refused" in result["error"]
        # The comment carrying the leak is never posted raw.
        host.post_pr_comment.assert_not_called()

    def test_clean_body_to_a_public_repo_posts_and_writes_a_send_audit_row(self) -> None:
        host = self._host()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=([], [])),
        ):
            result = cast("dict[str, object]", call_command("pr", "post-test-plan", "10", "--body", "all green on dev"))
        assert result == {"id": 1}
        host.post_pr_comment.assert_called_once()
        assert SendAudit.objects.filter(action="post_evidence").exists()

    def test_deprecated_post_evidence_alias_also_scans(self) -> None:
        host = self._host()
        self._monkeypatch.setattr(pr_command, "code_host_from_overlay", lambda: host)
        with (
            patch("teatree.core.overlay_loader._discover_overlays", return_value=_MOCK_OVERLAY),
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            result = cast(
                "dict[str, object]", call_command("pr", "post-evidence", "10", "--body", "shipping for Contoso")
            )
        assert "error" in result
        assert "privacy gate refused" in result["error"]
        host.post_pr_comment.assert_not_called()
