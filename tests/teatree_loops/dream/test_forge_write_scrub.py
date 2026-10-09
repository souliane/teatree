"""The dream loop's forge writers route through the shared #117 scrub seam (U14).

Before this seam, the dream loop's ``create_issue`` / ``update_issue`` writes ran
NO scrub — distilled-memory issues went to a public repo through an unscrubbed
path, and the MCP layer was stricter than core. Each write now routes through
:func:`teatree.core.send_proxy.route_forge_write`, so the public-repo leak gate +
the #117 send-proxy audit fire IDENTICALLY here and in the MCP tools. These tests
pin that: a SendAudit row is written for each forge write, and a leaking body is
SKIPped rather than reaching the backend. A binding reconciliation files nothing: it is
queued as a gap, and the fold that later carries it into a host is screened by the same seam.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import ConfigSetting, SendAudit, Ticket
from teatree.loops.dream import umbrella_ledger as ul
from teatree.loops.dream.binding_reconcile import queue_binding_reconciliations
from teatree.loops.dream.merge import BindingConflict

UMBRELLA = "https://github.com/souliane/teatree/issues/2663"
REPO = "souliane/teatree"


def _conflict() -> BindingConflict:
    return BindingConflict(
        survivor_name="feedback_bind_one",
        absorbed_name="feedback_bind_two",
        survivor_path=Path("/m/feedback_bind_one.md"),
        absorbed_path=Path("/m/feedback_bind_two.md"),
    )


def _fake_host(*, body: str = "## Open gaps\n") -> CodeHostBackend:
    host = MagicMock(spec=CodeHostBackend)
    host.search_open_issues.return_value = []
    # The #162 create-dedupe reads the whole open backlog before it files.
    host.list_repo_open_issues.return_value = []
    host.get_issue.return_value = {"body": body, "user": {"login": "souliane"}}
    host.current_user.return_value = "souliane"
    host.update_issue.return_value = {"number": 2663}
    host.create_issue.return_value = {"html_url": f"https://github.com/{REPO}/issues/9"}
    return host


@pytest.mark.usefixtures("configured_banned_term_registry")
class ReconciliationFilesNothing(TestCase):
    def test_a_binding_conflict_is_queued_and_never_reaches_the_forge(self) -> None:
        Ticket.objects.create(issue_url=UMBRELLA)
        host = _fake_host()

        outcomes = queue_binding_reconciliations(umbrella_url=UMBRELLA, conflicts=[_conflict()])

        assert outcomes[0].filed is True
        host.create_issue.assert_not_called()
        assert not SendAudit.objects.filter(action="dream_reconcile").exists()


class UmbrellaUpdateRoutesThroughSeam(TestCase):
    def test_upsert_writes_a_send_audit_row(self) -> None:
        ConfigSetting.objects.set_value("send_proxy_allowlist", [f"github:{REPO}"])
        host = _fake_host(body="## Open gaps\n")
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False):
            added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate")
        assert added is True
        host.update_issue.assert_called_once()
        assert SendAudit.objects.filter(destination=f"github:{REPO}", action="dream_umbrella_update").exists()

    def test_a_blocked_umbrella_write_skips_rather_than_crashing(self) -> None:
        host = _fake_host(body="## Open gaps\n")
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="g", title="ship for Contoso")
        assert added is False
        host.update_issue.assert_not_called()
