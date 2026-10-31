"""The board janitor's concurrent, deadline-bounded forge reads.

One pass measured 137 s against a 60 s scan deadline because every rule read the forge one
URL at a time, so the tick abandoned the scanner before any later rule applied a verdict.
The rule-level lanes (``test_board_reconcile*.py``) pin that each rule reads through this
helper; these pin the helper's own contract.
"""

import threading
from collections.abc import Callable
from time import monotonic
from unittest.mock import patch

from django.test import SimpleTestCase

from teatree.core.models import Ticket
from teatree.loop.scanners.board_reconcile_reads import (
    APPLY_MARGIN_SECONDS,
    READ_WORKERS,
    read_concurrently,
    read_per_overlay,
)
from teatree.loop.scanners.scan_deadline import scanner_deadline
from tests.teatree_loop._board_reconcile_clock import FakeClock, read_clock


class TestReadConcurrently(SimpleTestCase):
    def test_returns_the_completed_pairs_in_input_order(self) -> None:
        second_read_started = threading.Event()

        def read(item: int) -> int:
            if item == 0:
                assert second_read_started.wait(timeout=5)
            elif item == 1:
                second_read_started.set()
            return item * 10

        assert read_concurrently([0, 1, 2], read) == [(0, 0), (1, 10), (2, 20)]

    def test_a_raising_read_is_skipped_and_the_rest_are_kept(self) -> None:
        def read(item: int) -> int:
            if item == 1:
                msg = "forge blinked"
                raise RuntimeError(msg)
            return item

        with self.assertLogs("teatree.loop.scanners.board_reconcile_reads", level="WARNING") as logged:
            results = read_concurrently([0, 1, 2], read)

        assert results == [(0, 0), (2, 2)]
        assert any("2 of 3" in line for line in logged.output)

    def test_without_a_scan_deadline_every_item_is_read(self) -> None:
        items = list(range(READ_WORKERS * 3))

        assert read_concurrently(items, lambda item: item) == [(item, item) for item in items]

    def test_a_deadline_too_near_to_apply_issues_no_read_and_raises_nothing(self) -> None:
        for seconds_left in (-1, APPLY_MARGIN_SECONDS - 1):
            with self.subTest(seconds_left=seconds_left):
                reads: list[int] = []
                clock = FakeClock()

                with read_clock(clock), scanner_deadline(clock.now + seconds_left):
                    results = read_concurrently([1, 2, 3], reads.append)

                assert (results, reads) == ([], [])

    def test_a_deadline_that_arrives_mid_run_stops_the_reads_not_yet_started(self) -> None:
        clock = FakeClock()
        first_wave_in_flight = threading.Barrier(
            READ_WORKERS, timeout=5, action=lambda: clock.advance(APPLY_MARGIN_SECONDS + 31)
        )
        issued: list[int] = []

        def read(item: int) -> int:
            issued.append(item)
            if item < READ_WORKERS:
                assert clock.read_by_owner.wait(timeout=5)
                first_wave_in_flight.wait()
            return item

        with read_clock(clock), scanner_deadline(clock.now + APPLY_MARGIN_SECONDS + 30):
            results = read_concurrently(list(range(READ_WORKERS * 2)), read)

        assert sorted(issued) == list(range(READ_WORKERS))
        assert results == [(item, item) for item in range(READ_WORKERS)]

    def test_a_read_still_in_flight_at_the_deadline_does_not_hold_the_call(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        slow_read_returned = threading.Event()

        def read(item: int) -> int:
            if item == 0:
                release.wait(timeout=5)
                slow_read_returned.set()
            return item

        with scanner_deadline(monotonic() + APPLY_MARGIN_SECONDS + 0.5):
            results = read_concurrently([0, 1], read)

        assert not slow_read_returned.is_set()
        assert results == [(1, 1)]


class TestReadPerOverlay(SimpleTestCase):
    @staticmethod
    def _read(urls: list[str], url_of: Callable[[Ticket], str] | None = None) -> tuple[list[str], list[str]]:
        asked: list[str] = []

        def read(_overlay: object, url: str) -> str:
            asked.append(url)
            return url

        tickets = [Ticket(overlay="t3-teatree", issue_url=url) for url in urls]
        with patch("teatree.loop.scanners.board_reconcile_reads.overlay_loader.get_all_overlays") as overlays:
            overlays.return_value = {"t3-teatree": object()}
            pairs = (
                read_per_overlay(tickets, read) if url_of is None else read_per_overlay(tickets, read, url_of=url_of)
            )
        return asked, [ticket.issue_url for ticket, _ in pairs]

    def test_each_distinct_issue_url_is_read_once_through_its_own_overlay(self) -> None:
        asked, judged = self._read(["https://forge/x/issues/1", "https://forge/x/issues/1", "https://forge/x/issues/2"])

        assert sorted(asked) == ["https://forge/x/issues/1", "https://forge/x/issues/2"]
        assert len(judged) == 2

    def test_rows_that_a_url_hook_maps_to_one_url_share_one_read_of_that_url(self) -> None:
        urls = ["https://forge/x/issues/1#a", "https://forge/x/issues/1#b"]

        asked, judged = self._read(urls, url_of=lambda ticket: ticket.issue_url.partition("#")[0])

        assert asked == ["https://forge/x/issues/1"]
        assert judged == ["https://forge/x/issues/1#a"]

    def test_without_the_hook_two_fragments_are_two_reads_of_their_own_urls(self) -> None:
        asked, _ = self._read(["https://forge/x/issues/1#a", "https://forge/x/issues/1#b"])

        assert sorted(asked) == ["https://forge/x/issues/1#a", "https://forge/x/issues/1#b"]
