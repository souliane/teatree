"""The idle-reaper + queue-drainer mini-loops (#2190) build their jobs.

Mirrors the resource-pressure mini-loop: ``_build_jobs`` returns one global
``_ScannerJob`` when the builder yields a scanner, and an empty list when the
kill-switch disables it.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.loops.idle_stack_reaper.loop import MINI_LOOP as REAPER_LOOP
from teatree.loops.local_stack_queue.loop import MINI_LOOP as DRAINER_LOOP


class TestIdleStackReaperMiniLoop(TestCase):
    def test_name_and_cadence(self) -> None:
        assert REAPER_LOOP.name == "idle_stack_reaper"
        assert REAPER_LOOP.default_cadence_seconds == 60

    def test_build_jobs_wires_scanner(self) -> None:
        from teatree.loop.scanners import IdleStackReaperScanner  # noqa: PLC0415

        fake = IdleStackReaperScanner(overlay="t3-x")
        with patch(
            "teatree.loop.global_scanner_factories._idle_stack_reaper_scanner",
            return_value=fake,
        ):
            jobs = REAPER_LOOP.build_jobs()
        assert len(jobs) == 1
        assert jobs[0].scanner is fake
        assert jobs[0].overlay == ""

    def test_build_jobs_empty_when_disabled(self) -> None:
        with patch(
            "teatree.loop.global_scanner_factories._idle_stack_reaper_scanner",
            return_value=None,
        ):
            assert REAPER_LOOP.build_jobs() == []


class TestLocalStackQueueMiniLoop(TestCase):
    def test_name_and_cadence(self) -> None:
        assert DRAINER_LOOP.name == "local_stack_queue"
        assert DRAINER_LOOP.default_cadence_seconds == 60

    def test_build_jobs_wires_scanner(self) -> None:
        from teatree.loop.scanners import LocalStackQueueDrainerScanner  # noqa: PLC0415

        fake = LocalStackQueueDrainerScanner(overlay="t3-x")
        with patch(
            "teatree.loop.global_scanner_factories._local_stack_queue_drainer_scanner",
            return_value=fake,
        ):
            jobs = DRAINER_LOOP.build_jobs()
        assert len(jobs) == 1
        assert jobs[0].scanner is fake

    def test_build_jobs_empty_when_disabled(self) -> None:
        with patch(
            "teatree.loop.global_scanner_factories._local_stack_queue_drainer_scanner",
            return_value=None,
        ):
            assert DRAINER_LOOP.build_jobs() == []
