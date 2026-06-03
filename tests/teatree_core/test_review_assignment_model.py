"""Tests for :class:`ReviewAssignment` — the Slack-driven reviewer ledger (#1047)."""

from dataclasses import replace

import pytest

from teatree.core.models import ReviewAssignment, ReviewIntent

pytestmark = pytest.mark.django_db


MR = "https://gitlab.com/owner/repo/-/merge_requests/42"
USER = "U0A72P7CK0A"
BASE = ReviewIntent(mr_url=MR, user_id=USER, channel="C1", slack_ts="1.0", trigger="reaction")


class TestRecordIdempotency:
    def test_first_record_for_mr_creates_row(self) -> None:
        row = ReviewAssignment.record(BASE)

        assert row is not None
        assert row.state == ReviewAssignment.State.PENDING
        assert row.trigger == "reaction"
        assert ReviewAssignment.objects.count() == 1

    def test_same_mr_same_user_recorded_twice_yields_one_row(self) -> None:
        first = ReviewAssignment.record(BASE)
        second = ReviewAssignment.record(replace(BASE, slack_ts="2.0", trigger="mention"))

        assert first is not None
        assert second is None
        assert ReviewAssignment.objects.count() == 1

    def test_same_mr_different_user_yields_distinct_rows(self) -> None:
        ReviewAssignment.record(BASE)
        ReviewAssignment.record(replace(BASE, user_id="UOTHER"))

        assert ReviewAssignment.objects.count() == 2

    def test_different_mrs_yield_distinct_rows(self) -> None:
        ReviewAssignment.record(BASE)
        ReviewAssignment.record(replace(BASE, mr_url="https://gitlab.com/owner/repo/-/merge_requests/43"))

        assert ReviewAssignment.objects.count() == 2

    def test_different_overlays_with_same_mr_user_are_distinct(self) -> None:
        ReviewAssignment.record(replace(BASE, overlay="ovA"))
        ReviewAssignment.record(replace(BASE, channel="C2", slack_ts="2.0", overlay="ovB"))

        assert ReviewAssignment.objects.count() == 2

    def test_empty_mr_url_is_rejected(self) -> None:
        row = ReviewAssignment.record(replace(BASE, mr_url=""))
        assert row is None
        assert ReviewAssignment.objects.count() == 0

    def test_empty_user_id_is_rejected(self) -> None:
        row = ReviewAssignment.record(replace(BASE, user_id=""))
        assert row is None
        assert ReviewAssignment.objects.count() == 0


class TestStateTransitions:
    def _row(self) -> ReviewAssignment:
        row = ReviewAssignment.record(BASE)
        assert row is not None
        return row

    def test_mark_approved_stamps_timestamp(self) -> None:
        # #113/#86: the scanner no longer posts a discovery-time :eyes: claim,
        # so a row approves straight from PENDING (the approve path is
        # reachable from any non-approved state).
        row = self._row()
        assert row.mark_approved() is True
        assert row.state == ReviewAssignment.State.APPROVED
        assert row.approved_at is not None

    def test_mark_approved_is_no_op_when_already_approved(self) -> None:
        row = self._row()
        row.mark_approved()
        assert row.mark_approved() is False


class TestStrRepr:
    def test_repr_contains_state_and_mr(self) -> None:
        row = ReviewAssignment.record(BASE)
        assert row is not None
        text = str(row)
        assert "pending" in text
        assert MR in text
        assert USER in text


class TestApproveForMr:
    """The :meth:`approve_for_mr` classmethod closes the reaction → review → approve loop (#1047).

    Called from the ``PullRequest.approve`` signal.
    """

    def test_empty_mr_url_returns_zero(self) -> None:
        # Defensive guard: a transition with no URL should not scan the
        # whole table.
        assert ReviewAssignment.approve_for_mr(mr_url="") == 0

    def test_no_matching_rows_returns_zero(self) -> None:
        assert ReviewAssignment.approve_for_mr(mr_url=MR) == 0

    def test_approves_all_users_for_mr(self) -> None:
        # Two reviewers on the same MR — approve should advance both rows.
        ReviewAssignment.record(BASE)
        ReviewAssignment.record(replace(BASE, user_id="UOTHER"))

        count = ReviewAssignment.approve_for_mr(mr_url=MR)

        assert count == 2
        for row in ReviewAssignment.objects.filter(mr_url=MR):
            assert row.state == ReviewAssignment.State.APPROVED
            assert row.approved_at is not None

    def test_overlay_scoped(self) -> None:
        # Approve in ``ovA`` only — the ``ovB`` row stays pending.
        ReviewAssignment.record(replace(BASE, overlay="ovA"))
        ReviewAssignment.record(replace(BASE, channel="C2", slack_ts="2.0", overlay="ovB"))

        ReviewAssignment.approve_for_mr(mr_url=MR, overlay="ovA")

        a = ReviewAssignment.objects.get(overlay="ovA")
        b = ReviewAssignment.objects.get(overlay="ovB")
        assert a.state == ReviewAssignment.State.APPROVED
        assert b.state == ReviewAssignment.State.PENDING
