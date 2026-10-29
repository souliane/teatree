"""The explicit Slack step of a check-in."""

import json
import time
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.backends.slack.listener_heartbeat import ListenerBeat
from teatree.core import notify as notify_module
from teatree.core.checking_report.slack import ShootReportDict
from teatree.core.models import BotPing, DeferredQuestion, IncomingEvent
from teatree.core.models.scanner_fault import ScannerFault
from tests._owner_channel import owner_card
from tests.factories import TaskFactory, TicketFactory


def _shoot(beat: ListenerBeat | None = None, *, missing_listener: bool = False) -> ShootReportDict:
    out = StringIO()
    current_beat = (
        None
        if missing_listener
        else beat or ListenerBeat(updated_at=time.time(), interval_seconds=15, stamps={"test": time.time()})
    )
    with patch("teatree.core.management.commands.checking.read_heartbeat", return_value=current_beat):
        call_command("checking", "shoot", "--json", stdout=out, stderr=StringIO())
    return json.loads(out.getvalue())


class CheckingShootTestCase(TestCase):
    def test_stuck_owner_answer_uses_fallback(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            idempotency_key="stuck-answer",
            received_at=timezone.now() - timedelta(minutes=3),
        )
        backend = MagicMock(user_id="U_ME")

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            report = _shoot()

        assert report["mode"] == "fallback"
        assert "answers" in report["reason"]

    def test_recent_failed_post_is_retried_by_shoot(self) -> None:
        DeferredQuestion.record("Can I publish?", card=owner_card())
        BotPing.objects.create(
            idempotency_key="prior-failure", kind=BotPing.Kind.QUESTION, status=BotPing.Status.FAILED, text="failed"
        )
        backend = MagicMock(user_id="U_ME")
        backend.open_dm.return_value = "D_ME"
        ts = f"{timezone.now().timestamp():.6f}"
        backend.post_message.return_value = {"ok": True, "ts": ts}
        backend.get_permalink.return_value = f"https://slack.com/archives/D_ME/p{ts.replace('.', '')}"

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify_question_drains.owner_quiet_now", return_value=False),
        ):
            report = _shoot()

        assert report["posted"] == 1
        assert report["mode"] == "slack"

    def test_new_failed_post_flips_to_fallback_after_drain(self) -> None:
        backend = MagicMock(user_id="U_ME")

        def fail_post(**_kwargs: object) -> tuple[int, int]:
            BotPing.objects.create(
                idempotency_key="new-failure", kind=BotPing.Kind.QUESTION, status=BotPing.Status.NOOP, text="failed"
            )
            return 0, 1

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch("teatree.core.checking_report.slack.drain_unmirrored_deferred_questions", side_effect=fail_post),
        ):
            report = _shoot()

        assert report["mode"] == "fallback"
        assert report["reason"] == "Slack question post failed"

    def test_nudge_count_is_reported(self) -> None:
        backend = MagicMock(user_id="U_ME")

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch("teatree.core.checking_report.slack.reask_escalated_questions", return_value=(2, 2)),
        ):
            report = _shoot()

        assert report["nudged"] == 2

    def test_quiet_send_reports_why_card_is_still_waiting(self) -> None:
        DeferredQuestion.record("Can I publish?", card=owner_card())
        backend = MagicMock(user_id="U_ME")

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify_question_drains.owner_quiet_now", return_value=True),
        ):
            report = _shoot()

        assert report["mode"] == "slack"
        assert report["posted"] == 0
        assert report["remaining"] == 1
        assert "next pass" in report["reason"]

    def test_fallback_remaining_counts_only_unmirrored_cards(self) -> None:
        DeferredQuestion.record("Can I publish?", card=owner_card())
        DeferredQuestion.record(
            "Can I merge?", card=owner_card(), slack_channel="D_ME", slack_ts=f"{timezone.now().timestamp():.6f}"
        )

        with patch.object(notify_module, "messaging_from_overlay", return_value=None):
            report = _shoot()

        assert report["mode"] == "fallback"
        assert report["remaining"] == 1

    def test_missing_listener_uses_fallback_even_when_post_backend_exists(self) -> None:
        backend = MagicMock()
        backend.user_id = "U_ME"

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            report = _shoot(missing_listener=True)

        assert report["mode"] == "fallback"
        assert "listener" in report["reason"]
        backend.post_message.assert_not_called()

    def test_thread_link_label_uses_words_without_ticket_numbers(self) -> None:
        ticket = TicketFactory(short_description="dashboard 5789")
        task = TaskFactory(ticket=ticket, session__ticket=ticket)
        DeferredQuestion.record(
            "Can I publish it?",
            card=owner_card(),
            parked_task=task,
            slack_channel="D_ME",
            slack_ts=f"{timezone.now().timestamp():.6f}",
        )
        backend = MagicMock()
        backend.user_id = "U_ME"

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            report = _shoot()

        assert report["cards"][0]["label"] == "dashboard"

    def test_sibling_listener_can_process_owner_replies(self) -> None:
        backend = MagicMock()
        backend.user_id = "U_ME"
        now = time.time()
        beat = ListenerBeat(updated_at=now, interval_seconds=15, stamps={"other": now})

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch.dict("os.environ", {"T3_OVERLAY_NAME": "acme"}),
        ):
            report = _shoot(beat)

        assert report["mode"] == "slack"

    def test_stale_listener_recovers_and_heals_health_row(self) -> None:
        backend = MagicMock()
        backend.user_id = "U_ME"
        stale = ListenerBeat(updated_at=time.time() - 300, interval_seconds=15)

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            failed = _shoot(stale)
            healthy = _shoot()

        assert failed["mode"] == "fallback"
        assert failed["reason"] == "Slack answer listener is stale"
        assert healthy["mode"] == "slack"
        assert ScannerFault.objects.filter(scanner="checking:slack", healed_at__isnull=True).count() == 0

    def test_healthy_slack_posts_once_and_returns_only_thread_links(self) -> None:
        DeferredQuestion.record("Can I publish the prepared change?", card=owner_card())
        backend = MagicMock()
        backend.user_id = "U_ME"
        backend.open_dm.return_value = "D_ME"
        ts = f"{timezone.now().timestamp():.6f}"
        backend.post_message.return_value = {"ok": True, "ts": ts}
        backend.get_permalink.return_value = f"https://slack.com/archives/D_ME/p{ts.replace('.', '')}"

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify_question_drains.owner_quiet_now", return_value=False),
        ):
            first = _shoot()
            second = _shoot()

        assert first["mode"] == "slack"
        assert first["posted"] == 1
        assert first["cards"] == [
            {"label": "a waiting question", "permalink": f"https://slack.com/archives/D_ME/p{ts.replace('.', '')}"}
        ]
        assert second["posted"] == 0
        assert backend.post_message.call_count == 1
        assert BotPing.objects.filter(kind=BotPing.Kind.QUESTION).count() == 1
        assert "Can I publish" not in json.dumps(first)

    def test_missing_post_backend_falls_back_and_records_one_health_row(self) -> None:
        DeferredQuestion.record("Can I publish the prepared change?", card=owner_card())

        with patch.object(notify_module, "messaging_from_overlay", return_value=None):
            first = _shoot()
            second = _shoot()

        assert first["mode"] == second["mode"] == "fallback"
        assert "post" in first["reason"]
        assert ScannerFault.objects.filter(scanner="checking:slack", healed_at__isnull=True).count() == 1
        assert DeferredQuestion.owner_pending().count() == 1
        assert BotPing.objects.count() == 0
