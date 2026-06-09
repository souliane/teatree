"""The local-stack-queue-drainer mini-loop builds its global scanner job (#2190).

Mirrors ``src/teatree/loops/local_stack_queue/loop.py``. ``build_jobs`` returns
one global ``_ScannerJob`` when the factory yields a scanner, and an empty list
when the kill-switch disables it.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.loops.local_stack_queue.loop import MINI_LOOP as DRAINER_LOOP


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
