from datetime import timedelta
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.operational_health import collect_signals, reconcile_health
from teatree.core.models import TaskAttempt
from tests.factories import TaskFactory, TicketFactory


class StalledCheckoutTests(TestCase):
    def _park(self, *, age: timedelta) -> TaskAttempt:
        task = TaskFactory(ticket=TicketFactory(), phase="coding")
        attempt = TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now() - age,
            exit_code=0,
            error="checkout_parked: foreign edit",
            result={"summary": "checkout_parked: foreign edit"},
        )
        attempt.result["checkout_episode"] = f"attempt:{attempt.pk}"
        attempt.save(update_fields=["result"])
        return attempt

    def test_park_over_one_hour_is_health_signal_without_a_pr(self) -> None:
        attempt = self._park(age=timedelta(minutes=61))

        signals = [signal for signal in collect_signals().signals if signal.kind == "stalled_checkout"]
        assert len(signals) == 1
        assert signals[0].fingerprint == f"stalled-checkout:{attempt.task.ticket_id}"
        assert "foreign edit" in signals[0].summary

    def test_recent_park_is_not_stalled_and_success_clears_it(self) -> None:
        recent = self._park(age=timedelta(minutes=59))
        assert not [signal for signal in collect_signals().signals if signal.kind == "stalled_checkout"]
        recent.ended_at = timezone.now() - timedelta(minutes=61)
        recent.save(update_fields=["ended_at"])
        TaskAttempt.objects.create(task=recent.task, ended_at=timezone.now(), exit_code=0, result={"summary": "done"})
        assert not [signal for signal in collect_signals().signals if signal.kind == "stalled_checkout"]

    def test_new_episode_does_not_inherit_an_old_episode_age(self) -> None:
        first = self._park(age=timedelta(hours=2))
        second = TaskAttempt.objects.create(
            task=first.task,
            ended_at=timezone.now() - timedelta(minutes=59),
            exit_code=0,
            error="checkout_parked: foreign edit",
            result={"summary": "checkout_parked: foreign edit"},
        )
        second.result["checkout_episode"] = f"attempt:{second.pk}"
        second.save(update_fields=["result"])

        assert not [signal for signal in collect_signals().signals if signal.kind == "stalled_checkout"]

    def test_unfinished_park_uses_attempt_start_as_age(self) -> None:
        parked = self._park(age=timedelta(hours=2))
        TaskAttempt.objects.filter(pk=parked.pk).update(ended_at=None, started_at=timezone.now() - timedelta(hours=2))

        signals = [signal for signal in collect_signals().signals if signal.kind == "stalled_checkout"]
        assert len(signals) == 1

    def test_live_claim_suppresses_and_reconcile_clears_the_issue(self) -> None:
        parked = self._park(age=timedelta(hours=2))
        report = reconcile_health()
        assert len([issue for issue in report.open_issues if issue.kind == "stalled_checkout"]) == 1

        active = TaskFactory(ticket=parked.task.ticket, phase="testing")
        active.claim(claimed_by="worker", claimed_by_session="s1")
        report = reconcile_health()
        assert not [issue for issue in report.open_issues if issue.kind == "stalled_checkout"]

    def test_unreadable_source_is_reported_by_registry(self) -> None:
        with patch(
            "teatree.core.factory.stalled_checkout.TaskAttempt.objects.filter", side_effect=OperationalError("down")
        ):
            assert "stalled_checkout_signals" in collect_signals().unread
