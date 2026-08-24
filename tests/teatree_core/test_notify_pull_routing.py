"""Status signals are recorded and never DM'd; questions still land (#4524)."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import BotPing
from teatree.core.notify import NotifyKind, notify_user, notify_user_outcome
from teatree.core.notify_types import NotifyReason
from teatree.messaging.notify_with_fallback import NotifyTransport, notify_with_fallback


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


class TestStatusSignalsAreNotDMd(TestCase):
    def test_an_aged_skip_records_a_pulled_row_and_posts_nothing(self) -> None:
        backend = _backend()

        sent = notify_user(
            "PR souliane/teatree#4533 has been skipped by the merge sweep 3 consecutive times",
            kind=NotifyKind.INFO,
            idempotency_key="pr_sweep_aged_skip:souliane/teatree#4533:20684",
            audience=NotifyAudience.OWNER_ESCALATION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is False
        backend.open_dm.assert_not_called()
        backend.post_message.assert_not_called()
        row = BotPing.objects.get(idempotency_key="pr_sweep_aged_skip:souliane/teatree#4533:20684")
        assert row.status == BotPing.Status.PULLED
        assert row.audience == NotifyAudience.OWNER_ESCALATION.value
        assert "skipped by the merge sweep" in row.text

    def test_the_outcome_names_the_pull_surface(self) -> None:
        outcome = notify_user_outcome(
            "main is red",
            kind=NotifyKind.INFO,
            idempotency_key="pr_sweep_red_set:souliane/teatree:20260819",
            audience=NotifyAudience.OWNER_ESCALATION,
        )

        assert outcome.sent is False
        assert outcome.reason == NotifyReason.ROUTED_TO_PULL
        assert "notify digest" in outcome.detail

    def test_a_question_still_reaches_the_owner(self) -> None:
        backend = _backend()

        sent = notify_user(
            "investigate, rework or ignore?",
            kind=NotifyKind.QUESTION,
            idempotency_key="mirror-deferred-question:q-611",
            audience=NotifyAudience.OWNER_QUESTION,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        backend.post_message.assert_called_once()

    def test_a_registered_outage_page_still_reaches_the_owner(self) -> None:
        backend = _backend()

        sent = notify_user(
            "the compose stack is down",
            kind=NotifyKind.INFO,
            idempotency_key="watchdog:compose-up-failed:20260819",
            audience=NotifyAudience.OWNER_DELIVERY,
            backend=backend,
            user_id="U_ME",
        )

        assert sent is True
        backend.post_message.assert_called_once()

    def test_a_pulled_row_is_not_redelivered(self) -> None:
        notify_user(
            "status",
            kind=NotifyKind.INFO,
            idempotency_key="reconciliation:saturation:2026-08-19",
            audience=NotifyAudience.OWNER_ESCALATION,
            backend=_backend(),
            user_id="U_ME",
        )

        redeliverable = BotPing.objects.filter(BotPing.redeliverable_q()).values_list("idempotency_key", flat=True)
        assert "reconciliation:saturation:2026-08-19" not in set(redeliverable)

    def test_a_second_pull_of_the_same_key_does_not_duplicate_the_row(self) -> None:
        for _ in range(2):
            notify_user(
                "status",
                kind=NotifyKind.INFO,
                idempotency_key="pr-sweep-flag:souliane/teatree#4499:ci_red",
                audience=NotifyAudience.OWNER_ESCALATION,
                backend=_backend(),
                user_id="U_ME",
            )

        assert BotPing.objects.filter(idempotency_key="pr-sweep-flag:souliane/teatree#4499:ci_red").count() == 1


class TestFallbackDoesNotRescueAPulledSignal(TestCase):
    def test_the_fallback_stands_down_on_a_pull_verdict(self) -> None:
        with patch("teatree.messaging.notify_with_fallback._deliver_via_fallback") as deliver:
            result = notify_with_fallback(
                "PR skipped again",
                kind=NotifyKind.INFO,
                idempotency_key="pr_sweep_aged_skip:souliane/teatree#4533:20685",
                audience=NotifyAudience.OWNER_ESCALATION,
            )

        deliver.assert_not_called()
        assert result.delivered is False
        assert result.transport == NotifyTransport.NONE

    def test_it_stands_down_even_when_the_ledger_write_was_lost(self) -> None:
        """The row is what the recoverability probe reads, so an absent row must not fall open."""
        with (
            patch("teatree.core.notify.record_pulled"),
            patch("teatree.messaging.notify_with_fallback._deliver_via_fallback") as deliver,
        ):
            result = notify_with_fallback(
                "PR skipped again",
                kind=NotifyKind.INFO,
                idempotency_key="pr_sweep_aged_skip:souliane/teatree#4533:20686",
                audience=NotifyAudience.OWNER_ESCALATION,
            )

        assert BotPing.objects.filter(idempotency_key="pr_sweep_aged_skip:souliane/teatree#4533:20686").count() == 0
        deliver.assert_not_called()
        assert result.delivered is False
