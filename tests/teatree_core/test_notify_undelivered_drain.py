"""Re-delivery drain for INFO DMs stranded with no backend (sub-agent shell)."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing
from teatree.core.notify import drain_undelivered_notifies


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


class TestRecoverableInfo(TestCase):
    def test_noop_info_row_is_recoverable(self) -> None:
        BotPing.objects.create(
            idempotency_key="subagent-dm-1",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="task done",
            error_message="no messaging backend or user_id configured",
        )
        rows = list(BotPing.recoverable_info())
        assert [r.idempotency_key for r in rows] == ["subagent-dm-1"]

    def test_sent_info_row_is_not_recoverable(self) -> None:
        BotPing.objects.create(
            idempotency_key="already-sent",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.SENT,
            text="delivered",
        )
        assert list(BotPing.recoverable_info()) == []

    def test_question_kind_is_not_recovered_here(self) -> None:
        BotPing.objects.create(
            idempotency_key="deferred-q",
            kind=BotPing.Kind.QUESTION,
            status=BotPing.Status.NOOP,
            text="a question",
        )
        assert list(BotPing.recoverable_info()) == []

    def test_fresh_sending_is_not_recoverable(self) -> None:
        BotPing.objects.create(
            idempotency_key="in-flight",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.SENDING,
            text="being sent right now",
        )
        assert list(BotPing.recoverable_info()) == []

    def test_stale_sending_is_recoverable(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="crashed-claim",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.SENDING,
            text="claimed then crashed",
        )
        BotPing.objects.filter(pk=row.pk).update(
            posted_at=timezone.now() - BotPing.SENDING_STALE_AFTER - timedelta(seconds=1),
        )
        assert [r.idempotency_key for r in BotPing.recoverable_info()] == ["crashed-claim"]

    def test_row_older_than_age_cutoff_is_not_recoverable(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="weeks-stale",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="recorded weeks ago",
        )
        BotPing.objects.filter(pk=row.pk).update(
            posted_at=timezone.now() - BotPing.REDELIVERY_AGE_CUTOFF - timedelta(hours=1),
        )
        assert list(BotPing.recoverable_info()) == []

    def test_attempt_exhausted_row_is_not_recoverable(self) -> None:
        BotPing.objects.create(
            idempotency_key="exhausted",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="never delivers",
            attempts=BotPing.MAX_REDELIVERY_ATTEMPTS,
        )
        assert list(BotPing.recoverable_info()) == []

    def test_expired_row_is_not_recoverable(self) -> None:
        BotPing.objects.create(
            idempotency_key="abandoned",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.EXPIRED,
            text="given up on",
        )
        assert list(BotPing.recoverable_info()) == []


class TestExpireStaleInfo(TestCase):
    def test_aged_row_is_expired_terminally(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="aged",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="old noise",
        )
        BotPing.objects.filter(pk=row.pk).update(
            posted_at=timezone.now() - BotPing.REDELIVERY_AGE_CUTOFF - timedelta(hours=1),
        )
        assert BotPing.expire_stale_info() == 1
        assert BotPing.objects.get(pk=row.pk).status == BotPing.Status.EXPIRED

    def test_attempt_exhausted_row_is_expired_terminally(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="capped",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="hit the cap",
            attempts=BotPing.MAX_REDELIVERY_ATTEMPTS,
        )
        assert BotPing.expire_stale_info() == 1
        assert BotPing.objects.get(pk=row.pk).status == BotPing.Status.EXPIRED

    def test_fresh_under_cap_row_is_left_recoverable(self) -> None:
        BotPing.objects.create(
            idempotency_key="fresh",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="still worth retrying",
            attempts=1,
        )
        assert BotPing.expire_stale_info() == 0
        assert [r.idempotency_key for r in BotPing.recoverable_info()] == ["fresh"]

    def test_sent_row_is_never_expired(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="delivered",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.SENT,
            text="already gone",
        )
        BotPing.objects.filter(pk=row.pk).update(
            posted_at=timezone.now() - BotPing.REDELIVERY_AGE_CUTOFF - timedelta(hours=1),
        )
        assert BotPing.expire_stale_info() == 0
        assert BotPing.objects.get(pk=row.pk).status == BotPing.Status.SENT


class TestDrainUndeliveredNotifies(TestCase):
    def test_redelivers_stranded_info_dm_when_backend_now_available(self) -> None:
        BotPing.objects.create(
            idempotency_key="subagent-dm-2",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="sub-agent finished its on-behalf post",
            error_message="no messaging backend or user_id configured",
        )
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            delivered, total = drain_undelivered_notifies(user_id="U_ME")

        assert (delivered, total) == (1, 1)
        backend.post_message.assert_called_once()
        assert "sub-agent finished its on-behalf post" in backend.post_message.call_args.kwargs["text"]
        row = BotPing.objects.get(idempotency_key="subagent-dm-2")
        assert row.status == BotPing.Status.SENT

    def test_no_op_when_nothing_stranded(self) -> None:
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            assert drain_undelivered_notifies(user_id="U_ME") == (0, 0)
        backend.post_message.assert_not_called()

    def test_still_no_backend_bumps_attempt_and_keeps_row_recoverable_under_cap(self) -> None:
        BotPing.objects.create(
            idempotency_key="subagent-dm-3",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="still stranded",
        )
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
            delivered, total = drain_undelivered_notifies(user_id="U_ME")

        assert (delivered, total) == (0, 1)
        row = BotPing.objects.get(idempotency_key="subagent-dm-3")
        assert row.status == BotPing.Status.NOOP
        assert row.attempts == 1
        assert [r.idempotency_key for r in BotPing.recoverable_info()] == ["subagent-dm-3"]

    def test_repeated_no_backend_drains_eventually_expire_the_row(self) -> None:
        BotPing.objects.create(
            idempotency_key="never-deliverable",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="backend never resolves",
        )
        with patch("teatree.core.notify.messaging_from_overlay", return_value=None):
            for _ in range(BotPing.MAX_REDELIVERY_ATTEMPTS + 1):
                drain_undelivered_notifies(user_id="U_ME")

        row = BotPing.objects.get(idempotency_key="never-deliverable")
        assert row.status == BotPing.Status.EXPIRED
        assert list(BotPing.recoverable_info()) == []

        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            assert drain_undelivered_notifies(user_id="U_ME") == (0, 0)
        backend.post_message.assert_not_called()

    def test_aged_row_is_expired_without_any_delivery_attempt(self) -> None:
        row = BotPing.objects.create(
            idempotency_key="weeks-old",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="recorded weeks ago",
        )
        BotPing.objects.filter(pk=row.pk).update(
            posted_at=timezone.now() - BotPing.REDELIVERY_AGE_CUTOFF - timedelta(hours=1),
        )
        backend = _backend()
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            delivered, total = drain_undelivered_notifies(user_id="U_ME")

        assert (delivered, total) == (0, 0)
        backend.post_message.assert_not_called()
        assert BotPing.objects.get(pk=row.pk).status == BotPing.Status.EXPIRED

    def test_one_failure_does_not_abort_the_drain(self) -> None:
        for key in ("strand-a", "strand-b"):
            BotPing.objects.create(
                idempotency_key=key,
                kind=BotPing.Kind.INFO,
                status=BotPing.Status.NOOP,
                text=key,
            )
        backend = _backend()
        backend.post_message.side_effect = [
            {"ok": False, "error": "channel_not_found"},
            {"ok": True, "ts": "1700000000.000000"},
        ]
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            delivered, total = drain_undelivered_notifies(user_id="U_ME")

        assert total == 2
        assert delivered == 1

    def test_failed_delivery_preserves_prior_attempt_count(self) -> None:
        BotPing.objects.create(
            idempotency_key="backend-up-send-fails",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="backend resolves but the send breaks",
            attempts=3,
        )
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "channel_not_found"}
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            delivered, total = drain_undelivered_notifies(user_id="U_ME")

        assert (delivered, total) == (0, 1)
        row = BotPing.objects.get(idempotency_key="backend-up-send-fails")
        assert row.status == BotPing.Status.FAILED
        assert row.attempts == 4

    def test_failed_delivery_path_eventually_expires_at_the_cap(self) -> None:
        BotPing.objects.create(
            idempotency_key="backend-up-always-fails",
            kind=BotPing.Kind.INFO,
            status=BotPing.Status.NOOP,
            text="send keeps breaking",
        )
        backend = _backend()
        backend.post_message.return_value = {"ok": False, "error": "channel_not_found"}
        with patch("teatree.core.notify.messaging_from_overlay", return_value=backend):
            for _ in range(BotPing.MAX_REDELIVERY_ATTEMPTS + 1):
                drain_undelivered_notifies(user_id="U_ME")

        row = BotPing.objects.get(idempotency_key="backend-up-always-fails")
        assert row.status == BotPing.Status.EXPIRED
        assert list(BotPing.recoverable_info()) == []
