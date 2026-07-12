"""The unified MR-comment router runs the leak scan, not just the audit (CC-4).

``route_forge_send`` used to route a colleague-visible MR comment through the
send-proxy (:func:`teatree.core.send_proxy.route_send`) ONLY — no public-repo
leak scan — so it was a laxer path than every other forge writer, and a caller
that picked it silently skipped the leak gate. It now delegates to the ONE
scanned chokepoint (:func:`teatree.core.send_proxy.route_forge_write`), so a
leaking MR comment bound for a public repo is REFUSED before the wire call.
"""

from unittest.mock import patch

import pytest

from teatree.cli.review.send_routing import route_forge_send
from teatree.core.models import SendAudit

# route_forge_send delegates through route_forge_write -> route_send, which
# writes a SendAudit row, so the DB is required for the audited paths.
# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestRouteForgeSendLeakScan:
    def test_leaking_comment_to_a_public_repo_is_refused_before_the_wire(self) -> None:
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate._overlay_privacy_rules", return_value=(["Contoso"], [])),
        ):
            note, refusal = route_forge_send(
                repo="souliane/teatree", mr=7, action="post_comment", note="rolling out for Contoso"
            )
        # The seam refuses: the caller returns the refusal, the raw note is NOT posted.
        assert refusal
        assert "privacy gate refused" in refusal
        assert note == "rolling out for Contoso"
        # The leak gate refuses BEFORE the send-proxy audit, so nothing is audited.
        assert SendAudit.objects.count() == 0

    def test_clean_comment_passes_and_writes_a_send_audit_row(self) -> None:
        with patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False):
            note, refusal = route_forge_send(repo="souliane/teatree", mr=7, action="post_comment", note="all green")
        assert refusal == ""
        assert note == "all green"
        row = SendAudit.objects.get()
        assert row.channel == SendAudit.Channel.GITLAB.value
        assert row.action == "post_comment"
        assert row.target == "souliane/teatree!7"

    def test_empty_note_is_a_noop_passthrough_with_no_audit(self) -> None:
        note, refusal = route_forge_send(repo="souliane/teatree", mr=7, action="post_comment", note="")
        assert (note, refusal) == ("", "")
        assert SendAudit.objects.count() == 0
