"""The dream loop's forge writers route through the shared #117 scrub seam (U14).

Before this seam, the dream loop's ``create_issue`` / ``update_issue`` writes ran
NO scrub — distilled-memory issues went to a public repo through an unscrubbed
path, and the MCP layer was stricter than core. Each write now routes through
:func:`teatree.core.send_proxy.route_forge_write`, so the public-repo leak gate +
the #117 send-proxy audit fire IDENTICALLY here and in the MCP tools. These tests
pin that: a SendAudit row is written for each umbrella-update forge write, and a
leaking umbrella body is SKIPped rather than reaching the backend. Binding-
reconciliation no longer writes to the forge at all (#2663 dream-batch
dea750a552f8f2d2): a conflict is queued as a PendingArticleSuggestion for owner
approval instead, so its own class pins the queueing contract, not the scrub seam.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.backend_protocols import CodeHostBackend
from teatree.core.models import PendingArticleSuggestion, SendAudit
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


class ReconciliationQueuesForOwnerApproval(TestCase):
    """A conflict queues instead of filing (#2663 dream-batch dea750a552f8f2d2).

    It never reaches the forge — it is queued as a PendingArticleSuggestion and
    the batch-scanning operation never auto-creates an issue.
    """

    def test_a_conflict_is_queued_as_a_pending_suggestion_not_filed(self) -> None:
        host = _fake_host()
        outcomes = file_binding_reconciliation_tickets(host, repo=REPO, conflicts=[_conflict()])
        assert outcomes[0].filed is False
        assert outcomes[0].reason == "queued for owner approval (PendingArticleSuggestion)"
        host.create_issue.assert_not_called()
        assert SendAudit.objects.filter(destination=REPO).count() == 0
        row = PendingArticleSuggestion.objects.get(kind=PendingArticleSuggestion.Kind.MEMORY_RECONCILIATION)
        assert row.status == PendingArticleSuggestion.Status.PENDING

    def test_a_banned_term_in_the_rendered_body_withholds_the_candidate(self) -> None:
        host = _fake_host()
        with patch("teatree.loops.dream.promote_memory.banned_terms_scanner.scan_text", return_value="secret-token"):
            outcomes = file_binding_reconciliation_tickets(host, repo=REPO, conflicts=[_conflict()])
        assert outcomes[0].withheld is True
        assert "banned term" in (outcomes[0].reason or "")
        host.create_issue.assert_not_called()
        assert PendingArticleSuggestion.objects.count() == 0


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
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            added = ul.upsert_gap_checkbox(host, umbrella_url=UMBRELLA, gap_key="g", title="ship for Contoso")
        assert added is False
        host.update_issue.assert_not_called()
