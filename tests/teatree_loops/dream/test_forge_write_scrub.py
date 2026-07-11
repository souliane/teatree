"""The dream loop's forge writers route through the shared #117 scrub seam (U14).

Before this seam, the dream loop's ``create_issue`` / ``update_issue`` writes ran
NO scrub — distilled-memory issues went to a public repo through an unscrubbed
path, and the MCP layer was stricter than core. Each write now routes through
:func:`teatree.core.send_proxy.route_forge_write`, so the public-repo leak gate +
the #117 send-proxy audit fire IDENTICALLY here and in the MCP tools. These tests
pin that: a SendAudit row is written for each forge write, and a leaking body is
WITHHELD (reconciliation) / SKIPped (umbrella) rather than reaching the backend.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import SendAudit
from teatree.loops.dream import umbrella_ledger as ul
from teatree.loops.dream.merge import BindingConflict
from teatree.loops.dream.promote_memory import file_binding_reconciliation_tickets

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
    host.get_issue.return_value = {"body": body}
    host.update_issue.return_value = {"number": 2663}
    host.create_issue.return_value = {"html_url": f"https://github.com/{REPO}/issues/9"}
    return host


class ReconciliationRoutesThroughSeam(TestCase):
    def test_a_filed_reconciliation_issue_writes_a_send_audit_row(self) -> None:
        host = _fake_host()
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False):
            outcomes = file_binding_reconciliation_tickets(host, repo=REPO, conflicts=[_conflict()])
        assert outcomes[0].filed is True
        # The #117 send-proxy audited the outbound write (title + body → 2 rows).
        rows = SendAudit.objects.filter(destination=REPO)
        assert rows.count() >= 1
        assert rows.first().action == "dream_reconcile"

    def test_a_leaking_body_is_withheld_before_the_backend(self) -> None:
        # A public target + an overlay redact term the rendered body carries
        # ("BINDING", in the reconciliation title) ⇒ the leak gate refuses and the
        # issue is withheld, never reaching create_issue.
        host = _fake_host()
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["BINDING"], [])),
        ):
            outcomes = file_binding_reconciliation_tickets(host, repo=REPO, conflicts=[_conflict()])
        assert outcomes[0].withheld is True
        assert "privacy gate refused" in (outcomes[0].reason or "")
        host.create_issue.assert_not_called()


class UmbrellaUpdateRoutesThroughSeam(TestCase):
    def test_upsert_writes_a_send_audit_row(self) -> None:
        host = _fake_host(body="## Open gaps\n")
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False):
            added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="gap-1", title="Fix the gate")
        assert added is True
        host.update_issue.assert_called_once()
        assert SendAudit.objects.filter(destination=UMBRELLA, action="dream_umbrella_update").exists()

    def test_a_blocked_umbrella_write_skips_rather_than_crashing(self) -> None:
        host = _fake_host(body="## Open gaps\n")
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="g", title="ship for Contoso")
        assert added is False
        host.update_issue.assert_not_called()
