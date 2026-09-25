"""``Ticket.advance_to_delivered`` — the single transactional post-ship walk.

Owns the ``shipped → in_review → merged → retrospected`` FSM walk that both the
``sync-completions`` sweep and the loop's mechanical ``complete_ticket`` share.
Each step commits in its own ``atomic()``; a mid-chain gate/FSM refusal stops
the walk and is reported in the returned ``AdvanceResult`` — never raised.
"""

from django.test import TestCase

from teatree.core.models import ConfigSetting, Ticket
from teatree.core.models.ticket_state_sets import AdvanceResult


class TestAdvanceToDelivered(TestCase):
    def test_merged_ticket_walks_to_retrospected(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/1", state=Ticket.State.MERGED)

        result = ticket.advance_to_delivered()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETRO_RECORDED
        assert result == AdvanceResult(from_state="merged", to_state="retro_recorded")
        assert result.advanced
        assert not result.refused

    def test_shipped_ticket_walks_all_ungated_steps(self) -> None:
        # With the merge-evidence gate off (the default) the whole chain advances.
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/2", state=Ticket.State.PR_OPENED)

        result = ticket.advance_to_delivered()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.RETRO_RECORDED
        assert result.from_state == "pr_opened"
        assert result.to_state == "retro_recorded"
        assert not result.refused

    def test_mid_chain_refusal_reports_persisted_partial_state(self) -> None:
        # The merge-evidence gate refuses ``mark_merged`` for an evidence-less ticket.
        ConfigSetting.objects.set_value("require_merge_evidence", value=True)
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/3", state=Ticket.State.PR_OPENED)

        result = ticket.advance_to_delivered()

        ticket.refresh_from_db()
        # request_review committed in its own atomic step; mark_merged rolled back.
        assert ticket.state == Ticket.State.REVIEW_REQUESTED
        assert result.from_state == "pr_opened"
        assert result.to_state == "review_requested"
        assert result.refused
        assert result.error is not None
        assert "merged-SHA evidence" in result.error

    def test_non_completable_state_is_a_noop(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://x/4", state=Ticket.State.SCOPED)

        result = ticket.advance_to_delivered()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SCOPED
        assert result == AdvanceResult(from_state="scoped", to_state="scoped")
        assert not result.advanced
        assert not result.refused
