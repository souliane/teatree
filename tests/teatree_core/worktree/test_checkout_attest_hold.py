"""Checkout decisions wait eight minutes; other owner decisions can post at once."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing, DeferredQuestion, Worktree
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions, resurface_question_backlog
from teatree.core.worktree.checkout_attest import CheckoutAttestation, ask_owner_to_attest
from teatree.core.worktree.checkout_state import CheckoutState
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from tests._owner_channel import OWNER_DECISION
from tests.factories import TaskFactory, WorktreeFactory


class CheckoutAttestHoldTests(TestCase):
    def setUp(self) -> None:
        self.backend = MagicMock()
        self.backend.open_dm.return_value = "D-USER"
        self.backend.post_message.side_effect = lambda *_args, **_kwargs: {
            "ok": True,
            "ts": str(timezone.now().timestamp()),
        }
        self.backend.get_permalink.return_value = "https://example.slack.com/archives/D-USER/p1800000000000001"
        quiet = patch("teatree.core.notify_question_drains.owner_quiet_now", return_value=False)
        quiet.start()
        self.addCleanup(quiet.stop)

    def _ask(self) -> DeferredQuestion:
        task = TaskFactory()
        worktree = WorktreeFactory(ticket=task.ticket)
        card = ask_owner_to_attest(
            task, CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False)
        )
        assert card is not None
        return card

    def test_new_attest_waits_while_regular_decision_posts_immediately(self) -> None:
        attest = self._ask()
        regular = DeferredQuestion.record("Which option?", session_id="s", **OWNER_DECISION)

        assert drain_unmirrored_deferred_questions(user_id="U_ME", backend=self.backend) == (1, 1)
        attest.refresh_from_db()
        regular.refresh_from_db()
        assert not attest.slack_ts
        assert regular.slack_ts

        DeferredQuestion.objects.filter(pk=attest.pk).update(created_at=timezone.now() - timedelta(minutes=8))
        assert drain_unmirrored_deferred_questions(user_id="U_ME", backend=self.backend) == (1, 1)
        attest.refresh_from_db()
        assert attest.slack_ts

    def test_reasking_dismissed_state_gets_new_hold_and_delivery_key(self) -> None:
        first = self._ask()
        first.mark_stale("The owner decision was dismissed")
        worktree = Worktree.objects.get(ticket=first.parked_task.ticket)
        second = ask_owner_to_attest(
            first.parked_task,
            CheckoutAttestation(worktree, CheckoutState("d" * 64, 1, 1), host_evidence=False),
        )
        assert second is not None
        assert second.pk != first.pk
        assert second.stable_notify_ref != first.stable_notify_ref

        assert drain_unmirrored_deferred_questions(user_id="U_ME", backend=self.backend) == (0, 0)
        DeferredQuestion.objects.filter(pk=second.pk).update(created_at=timezone.now() - timedelta(minutes=8))
        assert drain_unmirrored_deferred_questions(user_id="U_ME", backend=self.backend) == (1, 1)
        keys = list(BotPing.objects.values_list("idempotency_key", flat=True))
        assert f"mirror-deferred-question:{second.stable_notify_ref}" in keys
        assert f"mirror-deferred-question:{first.stable_notify_ref}" not in keys

    def test_24_hour_checkout_replay_posts_only_the_decision_still_waiting_after_hold(self) -> None:
        start = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0)
        for index in range(28):
            asked_at = start + timedelta(minutes=index * 50)
            with patch.object(timezone, "now", return_value=asked_at):
                card = self._ask()
            if index != 12:
                card.apply_answer("Accept", resolved_via=DeferredQuestion.ResolvedVia.SLACK)

            eight_minutes_later = asked_at + timedelta(minutes=8)
            with patch.object(timezone, "now", return_value=eight_minutes_later):
                drain_unmirrored_deferred_questions(user_id="U_ME", backend=self.backend)
                posted, _pending = resurface_question_backlog(
                    user_id="U_ME", backend=self.backend, now=eight_minutes_later
                )
                assert not posted
                incidents = LifecycleIncidentDetector(now=lambda check_at=eight_minutes_later: check_at).detect()
                assert not [report for report in incidents if report.payload["kind"] == "outbound_unposted"]
            if index == 12:
                card.apply_answer("Accept", resolved_via=DeferredQuestion.ResolvedVia.SLACK)

        assert self.backend.post_message.call_count == 1
        assert "Should the " in str(self.backend.post_message.call_args)
