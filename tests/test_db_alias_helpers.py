"""``run_racing_threads`` surfaces a worker's exception (souliane/teatree#4010).

The runners it replaces collected outcomes into a dict and read misses back
through a default, so two threads dying on a stale test schema returned
``['', '']`` — indistinguishable from a decision the code under test made. The
guard's concurrent-claim test was duly filed as "mutual exclusion that excludes
everyone" against what was a schema gap in the test's own fixture.

A harness failure must look like a failure, never like an answer.
"""

import threading

import pytest

from tests.db_alias import run_racing_threads


class TestResults:
    def test_results_come_back_in_index_order(self) -> None:
        assert run_racing_threads(lambda idx: idx * 10, 3) == [0, 10, 20]

    def test_a_none_result_is_a_result_not_a_hole(self) -> None:
        assert run_racing_threads(lambda idx: None if idx else "post", 2) == ["post", None]

    def test_the_workers_run_concurrently(self) -> None:
        barrier = threading.Barrier(2, timeout=10)

        def work(idx: int) -> int:
            barrier.wait()
            return idx

        assert run_racing_threads(work, 2) == [0, 1]


class TestFailuresSurface:
    def test_a_worker_exception_is_raised_not_swallowed(self) -> None:
        def work(idx: int) -> str:
            if idx == 1:
                msg = "no such column: nag_count"
                raise RuntimeError(msg)
            return "post"

        with pytest.raises(RuntimeError, match="no such column"):
            run_racing_threads(work, 2)

    def test_a_worker_that_never_finishes_is_raised_not_swallowed(self) -> None:
        release = threading.Event()

        def work(idx: int) -> str:
            if idx == 1:
                release.wait(timeout=30)
            return "post"

        try:
            with pytest.raises(TimeoutError, match="worker 1"):
                run_racing_threads(work, 2, timeout=0.5)
        finally:
            release.set()
