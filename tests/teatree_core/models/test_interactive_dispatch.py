"""The interactive dispatch seat — the ledger the #4107 ceiling was missing (#4129)."""

import contextlib
import datetime as dt
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, BrokenBarrierError
from unittest.mock import patch

import pytest
from django.db import connections
from django.db.models import QuerySet
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import SEAT_WINDOW, InteractiveDispatch, InteractiveDispatchManager
from teatree.utils.thread_db import close_thread_db_connections
from tests.db_alias import register_control_db_alias, teardown_sqlite_alias, wal_control_db

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


class InteractiveDispatchAtomicityTests(TestCase):
    def test_failed_claim_leaves_no_orphan_seat(self) -> None:
        with (
            patch.object(InteractiveDispatch.objects, "live_seats", side_effect=RuntimeError("count failed")),
            pytest.raises(RuntimeError, match="count failed"),
        ):
            InteractiveDispatch.objects.claim_seat(session_id=SESSION, ceiling=1)

        assert InteractiveDispatch.objects.count() == 0

    def test_failed_release_does_not_persist_partial_release(self) -> None:
        seat = InteractiveDispatch.objects.record_seat(session_id=SESSION)
        original_save = InteractiveDispatch.save

        def save_then_fail(instance: InteractiveDispatch, *args: object, **kwargs: object) -> None:
            original_save(instance, *args, **kwargs)
            error = RuntimeError("release failed")
            raise error

        with (
            patch.object(InteractiveDispatch, "save", save_then_fail),
            pytest.raises(RuntimeError, match="release failed"),
        ):
            InteractiveDispatch.objects.release_seat(session_id=SESSION, agent_id="failed-agent")

        seat.refresh_from_db()
        assert seat.agent_id == ""
        assert seat.released_at is None


@pytest.fixture
def _private_sqlite_access(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_private_sqlite_access", wal_control_db.__name__)
class TestInteractiveDispatchConcurrentRelease:
    def test_two_releases_of_one_seat_succeed_exactly_once(self, tmp_path: Path) -> None:
        alias = f"seat_{uuid.uuid4().hex}"
        register_control_db_alias(alias, tmp_path / "seats.sqlite3")
        try:
            with connections[alias].schema_editor() as schema:
                schema.create_model(InteractiveDispatch)
            manager = InteractiveDispatch.objects.db_manager(alias)
            manager.record_seat(session_id=SESSION)
            barrier = Barrier(2)
            original_first = QuerySet.first

            def rendezvous_after_selection(queryset: QuerySet) -> InteractiveDispatch | None:
                seat = original_first(queryset)
                if queryset.model is InteractiveDispatch and seat is not None:
                    with contextlib.suppress(BrokenBarrierError):
                        barrier.wait(timeout=5)
                return seat

            def release(agent_id: str) -> bool:
                try:
                    return manager.release_seat(session_id=SESSION, agent_id=agent_id)
                finally:
                    close_thread_db_connections()

            with (
                patch.object(QuerySet, "first", rendezvous_after_selection),
                ThreadPoolExecutor(max_workers=2) as workers,
            ):
                results = list(workers.map(release, ("agent-one", "agent-two")))

            assert sorted(results) == [False, True]
            assert manager.live_seats().count() == 0
        finally:
            teardown_sqlite_alias(alias)
