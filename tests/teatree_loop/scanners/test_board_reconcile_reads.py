"""The board janitor's concurrent, deadline-bounded forge reads.

One pass measured 137 s against a 60 s scan deadline because every rule read the forge one
URL at a time, so the tick abandoned the scanner before any later rule applied a verdict.
The rule-level lanes (``test_board_reconcile*.py``) pin that each rule reads through this
helper; these pin the helper's own contract.
"""

import threading

from django.test import SimpleTestCase

from teatree.loop.scanners.board_reconcile_reads import APPLY_MARGIN_SECONDS, READ_WORKERS, read_concurrently
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
                first_wave_in_flight.wait()
            return item

        with read_clock(clock), scanner_deadline(clock.now + APPLY_MARGIN_SECONDS + 30):
            results = read_concurrently(list(range(READ_WORKERS * 2)), read)

        assert sorted(issued) == list(range(READ_WORKERS))
        assert results == [(item, item) for item in range(READ_WORKERS)]
