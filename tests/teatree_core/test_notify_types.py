"""Tests for teatree.core.notify_types — the notify-egress value vocabulary."""

from teatree.core.notify_types import NotifyOutcome, NotifyReason, blocked


def test_blocked_names_its_reason_as_a_not_sent_outcome() -> None:
    outcome = blocked(NotifyReason.NO_USER_ID)
    assert isinstance(outcome, NotifyOutcome)
    assert outcome.sent is False
    assert outcome.reason is NotifyReason.NO_USER_ID
    # detail falls through to the reason's canonical description
    assert outcome.detail == NotifyReason.NO_USER_ID.detail
    assert outcome.detail != ""


def test_blocked_carries_an_explicit_error_over_the_reason_detail() -> None:
    outcome = blocked(NotifyReason.LEDGER_UNAVAILABLE, error="database is locked")
    assert outcome.sent is False
    assert outcome.reason is NotifyReason.LEDGER_UNAVAILABLE
    # an explicit error wins over the reason's generic detail
    assert outcome.detail == "database is locked"
