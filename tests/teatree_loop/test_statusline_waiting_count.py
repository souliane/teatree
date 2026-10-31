"""The loop line counts only decisions addressed to the owner, read from the real rows (#5685)."""

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.review_assignment import ReviewAssignment, ReviewIntent
from teatree.core.models.waiting_item import WaitingItem
from teatree.core.waiting import gather_owner_waiting, gather_waiting
from teatree.loop.statusline import live_loops_anchor
from tests._owner_channel import owner_card


def _waiting_counts_on_the_loop_line() -> list[str]:
    acquired_at = datetime.now(UTC) - timedelta(seconds=120)
    with (
        patch("teatree.loop.statusline_loops._live_loop_leases", return_value=[("loop-tick", acquired_at)]),
        patch("teatree.loop.statusline_loops._cadence_for_loop", return_value=720),
    ):
        return re.findall(r"(\d+) waiting", "\n".join(live_loops_anchor()))


def _seed_waits_that_are_not_owner_cards() -> None:
    WaitingItem.objects.add("Review the queue")
    ReviewAssignment.record(
        ReviewIntent(
            mr_url="https://github.com/o/r/pull/7",
            user_id="u1",
            channel="C1",
            slack_ts="1.1",
            trigger="reaction",
            overlay="teatree",
        )
    )
    DeferredQuestion.objects.create(question="Legacy?", audience=DeferredQuestion.Audience.OWNER_QUESTION)


class TestTheLoopLineCountsOnlyWhatWaitsOnTheOwner(TestCase):
    def test_one_owner_card_among_other_waits_reads_one_waiting(self) -> None:
        DeferredQuestion.record("May I change the design?", card=owner_card())
        _seed_waits_that_are_not_owner_cards()

        assert len(gather_waiting("")) == 4
        assert len(gather_owner_waiting()) == 1
        assert _waiting_counts_on_the_loop_line() == ["1"]

    def test_waits_that_are_not_owner_cards_add_nothing_to_the_count(self) -> None:
        _seed_waits_that_are_not_owner_cards()

        assert len(gather_waiting("")) == 3
        assert gather_owner_waiting() == []
        assert _waiting_counts_on_the_loop_line() == []
