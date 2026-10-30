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
from teatree.core.models import BotPing, DeferredQuestion, IncomingEvent, PendingChatInjection
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


def _shoot_human() -> list[str]:
    err = StringIO()
    beat = ListenerBeat(updated_at=time.time(), interval_seconds=15, stamps={"test": time.time()})
    with patch("teatree.core.management.commands.checking.read_heartbeat", return_value=beat):
        call_command("checking", "shoot", stdout=StringIO(), stderr=err)
    return err.getvalue().splitlines()


class CheckingShootTestCase(TestCase):
    def test_finished_subject_is_settled_before_shoot_posts(self) -> None:
        ticket = TicketFactory()
        task = TaskFactory(ticket=ticket, session__ticket=ticket)
        question = DeferredQuestion.record("Can I publish?", parked_task=task, card=owner_card())
        ticket.state = ticket.State.DELIVERED
        ticket.save(update_fields=["state"])
        backend = MagicMock(user_id="U_ME")

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify_question_drains.owner_quiet_now", return_value=False),
        ):
            report = _shoot()

        question.refresh_from_db()
        assert report["posted"] == 0
        assert question.resolved_via == DeferredQuestion.ResolvedVia.STALE
        backend.post_message.assert_not_called()

    def test_stale_queued_owner_reply_uses_fallback(self) -> None:
        DeferredQuestion.record("Can I publish?", card=owner_card(), slack_channel="D_ME", slack_ts="1800000000.000001")
        PendingChatInjection.objects.create(
            channel="D_ME",
            slack_ts="1800000001.000001",
            thread_ts="1800000000.000001",
            user_id="U_ME",
            text="Yes",
            received_at=timezone.now() - timedelta(minutes=3),
        )
        backend = MagicMock(user_id="U_ME")

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            report = _shoot()

        assert report["mode"] == "fallback"
        assert "answers" in report["reason"]

    def test_unrelated_incoming_event_does_not_mark_slack_broken(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            idempotency_key="stuck-answer",
            received_at=timezone.now() - timedelta(minutes=3),
        )
        backend = MagicMock(user_id="U_ME")

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            report = _shoot()

        assert report["mode"] == "slack"

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

    def test_first_card_posts_during_quiet_hours(self) -> None:
        DeferredQuestion.record("Can I publish?", card=owner_card())
        backend = MagicMock(user_id="U_ME")
        backend.open_dm.return_value = "D_ME"
        ts = f"{timezone.now().timestamp():.6f}"
        backend.post_message.return_value = {"ok": True, "ts": ts}
        backend.get_permalink.return_value = f"https://slack.com/archives/D_ME/p{ts.replace('.', '')}"

        with (
            patch.object(notify_module, "messaging_from_overlay", return_value=backend),
            patch("teatree.core.notify_question_drains.owner_quiet_now", return_value=True),
        ):
            report = _shoot()

        assert report["mode"] == "slack"
        assert report["posted"] == 1
        assert report["remaining"] == 0

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

    def test_unposted_card_is_not_listed_as_a_thread_link(self) -> None:
        DeferredQuestion.record("Can I publish?", card=owner_card())
        DeferredQuestion.record("Can I merge?", card=owner_card(), slack_channel="D_ME", slack_ts="1800000000.000001")

        with patch.object(notify_module, "messaging_from_overlay", return_value=None):
            report = _shoot()

        assert report["cards"] == [
            {"label": "a waiting question", "permalink": "https://slack.com/archives/D_ME/p1800000000000001"}
        ]

    def test_human_output_gives_the_fallback_reason_the_links_and_the_unsent_count(self) -> None:
        DeferredQuestion.record("Can I publish?", card=owner_card())
        DeferredQuestion.record("Can I merge?", card=owner_card(), slack_channel="D_ME", slack_ts="1800000000.000001")

        with patch.object(notify_module, "messaging_from_overlay", return_value=None):
            lines = _shoot_human()

        assert lines == [
            "Slack post path is unavailable",
            "[a waiting question](https://slack.com/archives/D_ME/p1800000000000001)",
            "1 waiting for the next Slack send",
        ]

    def test_human_output_is_only_the_links_when_slack_works(self) -> None:
        DeferredQuestion.record("Can I merge?", card=owner_card(), slack_channel="D_ME", slack_ts="1800000000.000001")

        with patch.object(notify_module, "messaging_from_overlay", return_value=MagicMock(user_id="U_ME")):
            lines = _shoot_human()

        assert lines == ["[a waiting question](https://slack.com/archives/D_ME/p1800000000000001)"]

    def test_owner_reply_queued_for_under_two_minutes_is_not_a_broken_listener(self) -> None:
        DeferredQuestion.record("Can I publish?", card=owner_card(), slack_channel="D_ME", slack_ts="1800000000.000001")
        PendingChatInjection.objects.create(
            channel="D_ME",
            slack_ts="1800000001.000001",
            thread_ts="1800000000.000001",
            user_id="U_ME",
            text="Yes",
            received_at=timezone.now() - timedelta(minutes=1),
        )

        with patch.object(notify_module, "messaging_from_overlay", return_value=MagicMock(user_id="U_ME")):
            report = _shoot()

        assert report["mode"] == "slack"

    def test_paused_send_reason_needs_a_candidate_nothing_posted_and_a_card_still_waiting(self) -> None:
        def reason(*, posted: int, candidates: int) -> str:
            with (
                patch.object(notify_module, "messaging_from_overlay", return_value=MagicMock(user_id="U_ME")),
                patch(
                    "teatree.core.checking_report.slack.drain_unmirrored_deferred_questions",
                    return_value=(posted, candidates),
                ),
            ):
                report = _shoot()
            assert report["mode"] == "slack"
            return report["reason"]

        assert reason(posted=0, candidates=1) == ""
        DeferredQuestion.record("Can I publish?", card=owner_card())
        assert reason(posted=0, candidates=1) == "Slack send paused until the next pass"
        assert reason(posted=0, candidates=0) == ""
        assert reason(posted=1, candidates=1) == ""
