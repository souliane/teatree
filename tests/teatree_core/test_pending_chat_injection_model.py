"""Tests for :class:`PendingChatInjection` — the Slack-inbound queue (#1014)."""

import pytest
from django.utils import timezone

from teatree.core.models import PendingChatInjection

pytestmark = pytest.mark.django_db


class TestRecordIdempotency:
    """The scanner over-polls; ``record`` must be safe to call twice on the same ``ts``."""

    def test_same_ts_recorded_twice_yields_one_row(self) -> None:
        first = PendingChatInjection.record(
            channel="D0B36P8LU86", slack_ts="1700000000.0001", text="hello", user_id="U0A72P7CK0A"
        )
        second = PendingChatInjection.record(
            channel="D0B36P8LU86", slack_ts="1700000000.0001", text="hello", user_id="U0A72P7CK0A"
        )

        assert first is not None
        assert second is None
        assert PendingChatInjection.objects.count() == 1

    def test_distinct_ts_yields_distinct_rows(self) -> None:
        PendingChatInjection.record(channel="D0B36P8LU86", slack_ts="1.0", text="a")
        PendingChatInjection.record(channel="D0B36P8LU86", slack_ts="2.0", text="b")

        assert PendingChatInjection.objects.count() == 2

    def test_different_overlays_with_same_ts_are_distinct(self) -> None:
        PendingChatInjection.record(channel="C1", slack_ts="1.0", text="a", overlay="ovA")
        PendingChatInjection.record(channel="C2", slack_ts="1.0", text="b", overlay="ovB")

        assert PendingChatInjection.objects.count() == 2

    def test_empty_text_is_rejected(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="   ")

        assert row is None
        assert PendingChatInjection.objects.count() == 0

    def test_empty_ts_is_rejected(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="", text="hi")

        assert row is None
        assert PendingChatInjection.objects.count() == 0

    def test_empty_channel_is_rejected(self) -> None:
        row = PendingChatInjection.record(channel="", slack_ts="1.0", text="hi")

        assert row is None
        assert PendingChatInjection.objects.count() == 0


class TestPendingQuery:
    def test_pending_excludes_consumed_rows(self) -> None:
        consumed = PendingChatInjection.record(channel="C", slack_ts="1.0", text="old")
        assert consumed is not None
        consumed.consume()
        PendingChatInjection.record(channel="C", slack_ts="2.0", text="new")

        pending = list(PendingChatInjection.pending())
        assert [row.slack_ts for row in pending] == ["2.0"]

    def test_pending_returns_oldest_first(self) -> None:
        # Order by received_at; insert in reverse to prove ordering.
        later = PendingChatInjection.record(channel="C", slack_ts="2.0", text="later")
        earlier = PendingChatInjection.record(channel="C", slack_ts="1.0", text="earlier")
        assert later is not None
        assert earlier is not None
        # Force a deterministic received_at to remove insertion-time skew.
        earlier.received_at = timezone.now().replace(microsecond=0)
        later.received_at = earlier.received_at.replace(microsecond=1)
        earlier.save(update_fields=["received_at"])
        later.save(update_fields=["received_at"])

        pending = list(PendingChatInjection.pending())
        assert [row.slack_ts for row in pending] == ["1.0", "2.0"]

    def test_pending_filters_by_overlay_when_given(self) -> None:
        PendingChatInjection.record(channel="C", slack_ts="1.0", text="a", overlay="ovA")
        PendingChatInjection.record(channel="C", slack_ts="2.0", text="b", overlay="other")

        pending = list(PendingChatInjection.pending(overlay="ovA"))
        assert [row.overlay for row in pending] == ["ovA"]


class TestConsumeIdempotency:
    """The hook can re-fire safely — ``consume`` is single-use."""

    def test_first_consume_returns_true_and_stamps(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None

        assert row.consume() is True
        assert row.consumed_at is not None
        assert row.is_pending is False

    def test_second_consume_returns_false_no_op(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.consume()
        first_stamp = row.consumed_at

        assert row.consume() is False
        row.refresh_from_db()
        assert row.consumed_at == first_stamp


class TestStrRepr:
    def test_repr_for_pending_row(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x", overlay="ovA")
        assert row is not None
        assert "pending" in str(row)
        assert "ovA" in str(row)
        assert "1.0" in str(row)

    def test_repr_for_consumed_row(self) -> None:
        row = PendingChatInjection.record(channel="C", slack_ts="1.0", text="x")
        assert row is not None
        row.consume()
        assert "consumed" in str(row)
