"""Tests for teatree.core.notify_ledger — the notify-egress durable audit ledger."""

from unittest.mock import patch

from django.db import DatabaseError, IntegrityError
from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing, OutboundClaim
from teatree.core.notify_ledger import (
    already_sent_noop,
    claim_delivery_slot,
    finalize_failed,
    finalize_sent,
    maybe_stamp_answered,
    record_noop,
    record_outbound_claim,
)
from teatree.core.notify_types import NotifyKind, NotifyReason


class ClaimDeliverySlotTests(TestCase):
    KEY = "notify:test:1"

    def test_first_claim_wins_and_a_concurrent_reclaim_stands_down(self) -> None:
        # first caller wins the claim → proceed (None)
        assert claim_delivery_slot(self.KEY, kind="info", text="hi") is None
        assert BotPing.objects.get(idempotency_key=self.KEY).status == BotPing.Status.SENDING
        # a second claim while SENDING is in-flight → not-sent, named reason
        outcome = claim_delivery_slot(self.KEY, kind="info", text="hi")
        assert outcome is not None
        assert outcome.sent is False
        assert outcome.reason is NotifyReason.CLAIMED_BY_CONCURRENT_TICK

    def test_already_sent_is_an_idempotent_noop(self) -> None:
        assert claim_delivery_slot(self.KEY, kind="info", text="hi") is None
        finalize_sent(idempotency_key=self.KEY, channel="C1", posted_ts="1.1", permalink="http://x")
        assert BotPing.objects.get(idempotency_key=self.KEY).status == BotPing.Status.SENT
        # fast read-only idempotency no-op
        noop = already_sent_noop(self.KEY)
        assert noop is not None
        assert noop.sent is True
        assert noop.reason is NotifyReason.ALREADY_SENT
        # claim after SENT is also an already-sent no-op
        again = claim_delivery_slot(self.KEY, kind="info", text="hi")
        assert again is not None
        assert again.sent is True
        assert again.reason is NotifyReason.ALREADY_SENT

    def test_already_sent_noop_is_none_when_nothing_delivered(self) -> None:
        assert already_sent_noop("never-sent-key") is None


class FinalizeFailedTests(TestCase):
    KEY = "notify:test:fail"

    def test_finalize_failed_stamps_the_sending_row_recoverable(self) -> None:
        assert claim_delivery_slot(self.KEY, kind="info", text="hi") is None
        finalize_failed(idempotency_key=self.KEY, error="delivery rejected")
        row = BotPing.objects.get(idempotency_key=self.KEY)
        assert row.status == BotPing.Status.FAILED
        assert row.error_message == "delivery rejected"


class RecordNoopTests(TestCase):
    def test_record_noop_writes_a_noop_row_naming_the_reason(self) -> None:
        record_noop(
            idempotency_key="notify:noop:1",
            kind=NotifyKind.INFO,
            text="undelivered",
            audience=NotifyAudience.OWNER_ESCALATION,
            reason=NotifyReason.NO_MESSAGING_BACKEND,
        )
        row = BotPing.objects.get(idempotency_key="notify:noop:1")
        assert row.status == BotPing.Status.NOOP
        assert row.error_message == NotifyReason.NO_MESSAGING_BACKEND.detail


class MaybeStampAnsweredTests(TestCase):
    def test_a_non_answer_key_is_a_noop(self) -> None:
        with patch("teatree.core.models.PendingChatInjection.agent_answered_question") as stamp:
            maybe_stamp_answered(idempotency_key="info-not-an-answer", answering_slack_ts="")
        stamp.assert_not_called()

    def test_the_answer_key_convention_stamps_the_extracted_ts(self) -> None:
        with patch("teatree.core.models.PendingChatInjection.agent_answered_question") as stamp:
            maybe_stamp_answered(idempotency_key="answer-abc-1700000000.0001", answering_slack_ts="")
        stamp.assert_called_once_with("1700000000.0001")

    def test_an_explicit_ts_kwarg_wins_over_the_key(self) -> None:
        with patch("teatree.core.models.PendingChatInjection.agent_answered_question") as stamp:
            maybe_stamp_answered(idempotency_key="answer-abc-1700000000.0001", answering_slack_ts="1800000000.0002")
        stamp.assert_called_once_with("1800000000.0002")


class LedgerFailClosedTests(TestCase):
    def test_already_sent_noop_fails_closed_on_a_database_error(self) -> None:
        with patch.object(BotPing.objects, "filter", side_effect=DatabaseError("db locked")):
            outcome = already_sent_noop("some-key")
        assert outcome is not None
        assert outcome.sent is False
        assert outcome.reason is NotifyReason.LEDGER_UNAVAILABLE

    def test_claim_delivery_slot_fails_closed_on_a_database_error(self) -> None:
        with patch.object(BotPing, "claim_delivery", side_effect=DatabaseError("db locked")):
            outcome = claim_delivery_slot("k", kind="info", text="hi")
        assert outcome is not None
        assert outcome.reason is NotifyReason.LEDGER_UNAVAILABLE


class RecordOutboundClaimNeverRaisesTests(TestCase):
    def test_an_integrity_race_is_swallowed(self) -> None:
        with patch.object(OutboundClaim.objects, "get_or_create", side_effect=IntegrityError("dup")):
            record_outbound_claim(idempotency_key="k", target_url="u", channel="C1", posted_ts="1.1")
        # never-raise contract: the row simply is not written
        assert not OutboundClaim.objects.filter(idempotency_key="k").exists()

    def test_an_unexpected_error_is_swallowed(self) -> None:
        with patch.object(OutboundClaim.objects, "get_or_create", side_effect=ValueError("boom")):
            record_outbound_claim(idempotency_key="k2", target_url="u", channel="C1", posted_ts="1.1")
        assert not OutboundClaim.objects.filter(idempotency_key="k2").exists()
