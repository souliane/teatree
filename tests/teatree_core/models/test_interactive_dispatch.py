"""The interactive dispatch seat — the ledger the #4107 ceiling was missing (#4129)."""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import SEAT_WINDOW, InteractiveDispatch, InteractiveDispatchManager

SESSION = "sess-4129"
OTHER_SESSION = "sess-other"


class InteractiveDispatchSeatTests(TestCase):
    def test_the_default_manager_owns_the_seat_operations(self) -> None:
        assert isinstance(InteractiveDispatch.objects, InteractiveDispatchManager)

    def test_a_claimed_seat_is_live(self) -> None:
        assert InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=1) is True
        assert InteractiveDispatch.objects.live_seats().count() == 1

    def test_a_seat_past_the_window_is_no_longer_live(self) -> None:
        InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=1)
        later = timezone.now() + SEAT_WINDOW + dt.timedelta(seconds=1)
        assert InteractiveDispatch.objects.live_seats(now=later).count() == 0

    def test_a_released_seat_is_no_longer_live(self) -> None:
        InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=1)
        InteractiveDispatch.objects.release_seat(session_id=SESSION, agent_id="a-1")
        assert InteractiveDispatch.objects.live_seats().count() == 0

    def test_the_seat_beyond_the_ceiling_is_refused_and_leaves_no_row(self) -> None:
        assert InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=1) is True
        assert InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=1) is False
        assert InteractiveDispatch.objects.count() == 1

    def test_seats_are_counted_across_sessions(self) -> None:
        # The ceiling bounds the BOX, so one session's dispatches are visible to another's.
        InteractiveDispatch.objects.claim_seat(session_id=OTHER_SESSION, ceiling=1)
        assert InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=1) is False

    def test_agents_outside_the_ledger_spend_the_same_ceiling(self) -> None:
        # A durable Task claim is an agent on the same box; the rank cannot see it.
        assert InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=2, other_agents=2) is False

    def test_a_recorded_seat_clears_no_ceiling_but_is_counted(self) -> None:
        InteractiveDispatch.objects.record_seat(session_id=SESSION)
        InteractiveDispatch.objects.record_seat(session_id=SESSION)
        assert InteractiveDispatch.objects.live_seats().count() == 2
        assert InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=2) is False

    def test_an_expired_seat_is_pruned_by_the_next_dispatch(self) -> None:
        InteractiveDispatch.objects.record_seat(session_id=SESSION)
        InteractiveDispatch.objects.all().update(admitted_at=timezone.now() - SEAT_WINDOW - dt.timedelta(seconds=1))
        InteractiveDispatch.objects.record_seat(session_id=SESSION)
        assert InteractiveDispatch.objects.count() == 1

    def test_the_str_names_the_session(self) -> None:
        seat = InteractiveDispatch.objects.record_seat(session_id=SESSION)
        assert SESSION in str(seat)
        assert "(no session)" in str(InteractiveDispatch.objects.record_seat(session_id=""))


class InteractiveDispatchReleaseTests(TestCase):
    def test_the_oldest_seat_is_handed_back_first(self) -> None:
        oldest = InteractiveDispatch.objects.record_seat(session_id=SESSION)
        InteractiveDispatch.objects.record_seat(session_id=SESSION)
        assert InteractiveDispatch.objects.release_seat(session_id=SESSION, agent_id="a-1") is True
        oldest.refresh_from_db()
        assert oldest.released_at is not None
        assert oldest.agent_id == "a-1"

    def test_a_re_fired_stop_gives_nothing_further_back(self) -> None:
        InteractiveDispatch.objects.record_seat(session_id=SESSION)
        InteractiveDispatch.objects.record_seat(session_id=SESSION)
        assert InteractiveDispatch.objects.release_seat(session_id=SESSION, agent_id="a-1") is True
        assert InteractiveDispatch.objects.release_seat(session_id=SESSION, agent_id="a-1") is False
        assert InteractiveDispatch.objects.live_seats().count() == 1

    def test_a_blank_agent_id_releases_nothing(self) -> None:
        InteractiveDispatch.objects.record_seat(session_id=SESSION)
        assert InteractiveDispatch.objects.release_seat(session_id=SESSION, agent_id="") is False
        assert InteractiveDispatch.objects.live_seats().count() == 1

    def test_a_session_holding_no_seat_releases_nothing(self) -> None:
        InteractiveDispatch.objects.record_seat(session_id=OTHER_SESSION)
        assert InteractiveDispatch.objects.release_seat(session_id=SESSION, agent_id="a-1") is False
        assert InteractiveDispatch.objects.live_seats().count() == 1
