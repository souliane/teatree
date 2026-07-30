"""The per-send audit ledger the send-proxy writes (#117, migration M-C / 0041)."""

from django.test import TestCase

from teatree.core.models import SendAudit
from teatree.core.models.provenance import Provenance


class TestSendAuditRow(TestCase):
    def test_persists_the_delegation_provenance(self) -> None:
        SendAudit.objects.create(
            channel=SendAudit.Channel.GITLAB.value,
            destination="org/repo",
            action="post_comment",
            target="org/repo!42",
            overlay="acme",
            mode="warn",
            allowlist_verdict=SendAudit.Verdict.WARNED.value,
            redaction_applied=False,
            redaction_matches=[],
            provenance=Provenance.OWNER.value,
            authorized_by="ticket:117",
            agent_session_id="sess-1",
            payload_summary="LGTM",
        )
        stored = SendAudit.objects.get()
        assert stored.channel == "gitlab"
        assert stored.authorized_by == "ticket:117"
        assert stored.provenance == Provenance.OWNER.value
        assert stored.allowlist_verdict == "warned"


class TestSendAuditStr:
    def test_str_is_human_scannable(self) -> None:
        row = SendAudit(
            channel="slack",
            destination="C_TEAM",
            allowlist_verdict="denied",
            mode="enforce",
        )
        assert "C_TEAM" in str(row)
        assert "denied" in str(row)
