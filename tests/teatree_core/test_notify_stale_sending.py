"""A stale SENDING claim must not permanently block delivery for a reused key.

The double-DM TOCTOU fix claims a SENDING ``BotPing`` row before delivering and
finalizes it to SENT/FAILED after. A crash between claim and finalize leaves the
SENDING row stranded. Day-granular idempotency keys are reused by design — the
loop summary path keys on ``loops_tick_errors:{utc_day}`` /
``loops_tick_summary:{utc_day}`` (one row per UTC day, deduped by the ledger),
so the SAME key is presented many times a day.

Without a staleness escape, a stranded SENDING row blocks BOTH paths for the
rest of the day: ``claim_delivery`` returns IN_FLIGHT (SENDING is not
recoverable) so the primary never delivers, and
``notify_with_fallback._primary_failure_is_recoverable`` returns ``False`` for a
SENDING row so the fallback never delivers either. One crash mid-delivery would
silently swallow e.g. the loop-error summary DM for the rest of the day.

The fix: a SENDING row older than ``BotPing.SENDING_STALE_AFTER`` is treated as
recoverable in both decision points, so a later same-key call re-claims and
delivers. A FRESH SENDING row (a genuine concurrent in-flight delivery) must
STILL block — that is the legitimate double-DM guard.
"""

from datetime import timedelta
from unittest.mock import MagicMock

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing, DeliveryClaim
from teatree.core.notify import NotifyKind, notify_user
from teatree.messaging.notify_with_fallback import _primary_failure_is_recoverable

_KEY = "loops_tick_errors:2026-06-04"


def _backend() -> MagicMock:
    b = MagicMock()
    b.open_dm.return_value = "D-USER"
    b.post_message.return_value = {"ok": True, "ts": "1700000000.000000"}
    b.get_permalink.return_value = "https://acme.slack.com/archives/D-USER/p1700000000000000"
    return b


def _seed_sending(*, age: timedelta) -> BotPing:
    row = BotPing.objects.create(
        idempotency_key=_KEY,
        kind=BotPing.Kind.INFO.value,
        status=BotPing.Status.SENDING,
        text="claimed but never finalized (crash)",
    )
    BotPing.objects.filter(pk=row.pk).update(posted_at=timezone.now() - age)
    row.refresh_from_db()
    return row


class TestStaleSendingClaim(TestCase):
    def test_stale_sending_is_reclaimable_by_claim_delivery(self) -> None:
        _seed_sending(age=BotPing.SENDING_STALE_AFTER + timedelta(minutes=1))
        claim = BotPing.claim_delivery(_KEY, kind="info", text="retry after crash")
        assert claim == DeliveryClaim.CLAIMED
        assert BotPing.objects.filter(idempotency_key=_KEY, status=BotPing.Status.SENDING).count() == 1

    def test_fresh_sending_still_blocks_claim_delivery(self) -> None:
        _seed_sending(age=timedelta(seconds=1))
        claim = BotPing.claim_delivery(_KEY, kind="info", text="concurrent in-flight")
        assert claim == DeliveryClaim.IN_FLIGHT

    def test_stale_sending_does_not_block_notify_user_delivery(self) -> None:
        _seed_sending(age=BotPing.SENDING_STALE_AFTER + timedelta(minutes=1))
        backend = _backend()
        sent = notify_user(
            "loop error summary",
            kind=NotifyKind.INFO,
            idempotency_key=_KEY,
            backend=backend,
            user_id="U_ME",
        )
        assert sent is True
        backend.post_message.assert_called_once()
        assert BotPing.objects.get(idempotency_key=_KEY).status == BotPing.Status.SENT

    def test_fresh_sending_blocks_notify_user_delivery(self) -> None:
        _seed_sending(age=timedelta(seconds=1))
        backend = _backend()
        sent = notify_user(
            "would be a double-DM",
            kind=NotifyKind.INFO,
            idempotency_key=_KEY,
            backend=backend,
            user_id="U_ME",
        )
        assert sent is False
        backend.post_message.assert_not_called()

    def test_stale_sending_is_recoverable_for_fallback(self) -> None:
        _seed_sending(age=BotPing.SENDING_STALE_AFTER + timedelta(minutes=1))
        assert _primary_failure_is_recoverable(_KEY) is True

    def test_fresh_sending_is_not_recoverable_for_fallback(self) -> None:
        _seed_sending(age=timedelta(seconds=1))
        assert _primary_failure_is_recoverable(_KEY) is False
