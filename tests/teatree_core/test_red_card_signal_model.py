"""Tests for :class:`RedCardSignal` — the user RED CARD ledger (#1130).

A RED CARD is the user's signal that the agent did something structurally
wrong and must fix it *upstream in teatree*, not just behaviourally. The
model captures the three surfaces the user uses (``:red_circle:``
reaction, ``:no_entry_sign:`` reaction, or the literal phrase
``"RED CARD"`` in a DM/thread reply), each idempotent on
``(overlay, channel, slack_ts)``.
"""

from dataclasses import replace

import pytest

from teatree.core.models import RedCardIntent, RedCardSignal

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


USER = "U0DEMOUSER1"
CHANNEL = "D0DEMOTEAM1"
BASE = RedCardIntent(
    overlay="teatree",
    channel=CHANNEL,
    slack_ts="1779180558.938799",
    signal_kind=RedCardSignal.Kind.RED_CIRCLE,
    user_id=USER,
    offending_message_ts="1779180557.000100",
    offending_message_text="agent's previous reply that was wrong",
    signal_text=":red_circle:",
)


class TestRecordIdempotency:
    def test_first_record_creates_pending_row(self) -> None:
        row = RedCardSignal.record(BASE)

        assert row is not None
        assert row.state == RedCardSignal.State.PENDING
        assert row.signal_kind == RedCardSignal.Kind.RED_CIRCLE
        assert row.user_id == USER
        assert row.channel == CHANNEL
        assert row.overlay == "teatree"
        assert row.offending_message_text == "agent's previous reply that was wrong"
        assert row.signal_text == ":red_circle:"
        assert row.filed_issue_url == ""
        assert RedCardSignal.objects.count() == 1

    def test_same_overlay_channel_ts_recorded_twice_yields_one_row(self) -> None:
        first = RedCardSignal.record(BASE)
        second = RedCardSignal.record(replace(BASE, signal_kind=RedCardSignal.Kind.NO_ENTRY_SIGN))

        assert first is not None
        assert second is None
        assert RedCardSignal.objects.count() == 1

    def test_different_ts_yield_distinct_rows(self) -> None:
        RedCardSignal.record(BASE)
        RedCardSignal.record(replace(BASE, slack_ts="2.0"))

        assert RedCardSignal.objects.count() == 2

    def test_different_overlays_with_same_channel_ts_are_distinct(self) -> None:
        RedCardSignal.record(replace(BASE, overlay="ovA"))
        RedCardSignal.record(replace(BASE, overlay="ovB"))

        assert RedCardSignal.objects.count() == 2

    def test_empty_ts_is_rejected(self) -> None:
        assert RedCardSignal.record(replace(BASE, slack_ts="")) is None
        assert RedCardSignal.objects.count() == 0

    def test_empty_channel_is_rejected(self) -> None:
        assert RedCardSignal.record(replace(BASE, channel="")) is None
        assert RedCardSignal.objects.count() == 0


class TestStateTransitions:
    def _row(self) -> RedCardSignal:
        row = RedCardSignal.record(BASE)
        assert row is not None
        return row

    def test_mark_eyes_added_transitions_pending_row(self) -> None:
        row = self._row()
        assert row.mark_eyes_added() is True
        assert row.state == RedCardSignal.State.EYES_ADDED
        assert row.eyes_reacted_at is not None

    def test_mark_eyes_added_is_no_op_after_first(self) -> None:
        row = self._row()
        row.mark_eyes_added()
        assert row.mark_eyes_added() is False

    def test_link_issue_stamps_url_and_advances_state(self) -> None:
        row = self._row()
        url = "https://github.com/souliane/teatree/issues/9999"
        assert row.link_issue(url) is True
        assert row.filed_issue_url == url
        assert row.state == RedCardSignal.State.ISSUE_FILED

    def test_link_issue_after_eyes_added_advances(self) -> None:
        row = self._row()
        row.mark_eyes_added()
        url = "https://github.com/souliane/teatree/issues/9999"
        assert row.link_issue(url) is True
        assert row.state == RedCardSignal.State.ISSUE_FILED

    def test_link_issue_with_empty_url_is_rejected(self) -> None:
        row = self._row()
        assert row.link_issue("") is False
        assert row.state == RedCardSignal.State.PENDING

    def test_link_issue_on_resolved_row_is_no_op(self) -> None:
        row = self._row()
        # Force the row into RESOLVED state directly — the resolution
        # path is owned by the upstream-fix tracker, not the scanner.
        RedCardSignal.objects.filter(pk=row.pk).update(state=RedCardSignal.State.RESOLVED)
        row.refresh_from_db()
        assert row.link_issue("https://github.com/souliane/teatree/issues/9999") is False
        assert row.state == RedCardSignal.State.RESOLVED


class TestStrRepr:
    def test_repr_contains_state_kind_and_user(self) -> None:
        row = RedCardSignal.record(BASE)
        assert row is not None
        text = str(row)
        assert "pending" in text
        assert RedCardSignal.Kind.RED_CIRCLE.value in text
        assert USER in text
