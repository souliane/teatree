"""§17.6 enforcement candidates 9 & 13 + the `ticket merge` keystone CLI.

Candidate 13: ``lifecycle visit-phase <id> reviewing`` MUST require an
explicit ``--agent-id`` and reject maker / coding-agent / loop roles, and
overwrite-or-error on an existing ``reviewing`` key (never idempotent silent
false-success).

Candidate 9: ``lifecycle clear-ledger`` is the sanctioned session-retire path
so a reused ticket's stale phase ledger is cleared via ``t3``, not by
hand-editing state (which invariant 8 prohibits).
"""

from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands.lifecycle import ReviewerAttestationError
from teatree.core.models import MergeClear, Session, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestReviewingRequiresExplicitReviewer(TestCase):
    def test_reviewing_without_agent_id_is_refused(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        Session.objects.create(ticket=ticket, agent_id="maker:coding")
        with pytest.raises(ReviewerAttestationError, match="explicit"):
            call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing")

    def test_maker_role_agent_id_is_refused(self) -> None:
        for maker_id in ("maker:coding", "maker-1", "coding-agent", "coding", "loop"):
            with self.subTest(maker_id=maker_id):
                ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
                Session.objects.create(ticket=ticket, agent_id="maker:coding")
                with pytest.raises(ReviewerAttestationError, match="maker/coding-agent/loop"):
                    call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id=maker_id)

    def test_independent_reviewer_is_accepted(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        Session.objects.create(ticket=ticket, agent_id="maker:coding")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer")
        session = ticket.sessions.first()
        assert session is not None
        assert session.phase_visits["reviewing"]["agent_id"] == "cold-reviewer"

    def test_existing_reviewing_key_is_overwritten_loudly_not_silent(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        Session.objects.create(ticket=ticket, agent_id="maker:coding")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="reviewer-one")
        with self.assertLogs("teatree.core.management.commands.lifecycle", level="WARNING") as cm:
            call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="reviewer-two")
        session = ticket.sessions.first()
        assert session is not None
        assert session.phase_visits["reviewing"]["agent_id"] == "reviewer-two"
        assert any("Overwriting existing 'reviewing'" in line for line in cm.output)


class TestClearLedger(TestCase):
    def test_clear_ledger_requires_confirm(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="maker")
        session.visit_phase("coding", agent_id="maker")
        result = call_command("lifecycle", "clear-ledger", str(ticket.pk))
        session.refresh_from_db()
        assert "coding" in session.visited_phases
        assert "--confirm" in str(result)

    def test_clear_ledger_wipes_every_session_phase_ledger(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.STARTED)
        s1 = Session.objects.create(ticket=ticket, agent_id="maker")
        s1.visit_phase("coding", agent_id="maker")
        s1.visit_phase("testing", agent_id="maker")
        s2 = Session.objects.create(ticket=ticket, agent_id="reviewer")
        s2.visit_phase("reviewing", agent_id="reviewer")
        call_command("lifecycle", "clear-ledger", str(ticket.pk), confirm=True)
        s1.refresh_from_db()
        s2.refresh_from_db()
        assert s1.visited_phases == []
        assert s1.phase_visits == {}
        assert s2.visited_phases == []


class TestTicketMergeKeystoneCli(TestCase):
    def _gh_stub(self, argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if "headRefOid" in joined:
            return (0, "c" * 40, "")
        if "isDraft" in joined:
            return (0, "false", "")
        if "statusCheckRollup" in joined:
            return (0, '[{"status": "COMPLETED", "conclusion": "SUCCESS"}]', "")
        if "pulls" in joined and "merge" in joined:
            return (0, '{"sha": "landed00"}', "")
        return (0, "", "")

    def test_ticket_merge_advances_in_review_to_merged(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=859,
            slug="souliane/teatree",
            reviewed_sha="c" * 40,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.DOCS,
        )
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=self._gh_stub):
            result = cast(
                "dict[str, object]", call_command("ticket", "merge", str(clear.pk), loop_identity="merge-loop")
            )
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.MERGED
        assert result["merged"]

    def test_ticket_merge_missing_clear_is_reported(self) -> None:
        result = cast("dict[str, object]", call_command("ticket", "merge", "99999"))
        assert not (result["merged"])
        assert "not found" in result["error"]

    def test_ticket_merge_substrate_clear_re_escalates_without_fsm_change(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.IN_REVIEW)
        clear = MergeClear.objects.create(
            ticket=ticket,
            pr_id=860,
            slug="souliane/teatree",
            reviewed_sha="c" * 40,
            reviewer_identity="cold-reviewer",
            gh_verify_result=MergeClear.VerifyResult.GREEN,
            blast_class=MergeClear.BlastClass.SUBSTRATE,
        )
        with patch("teatree.backends.forge_merge_rpc.gh_runner", return_value=self._gh_stub):
            result = cast("dict[str, object]", call_command("ticket", "merge", str(clear.pk)))
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW
        assert result["escalated"]
        assert not (result["merged"])
