from unittest.mock import patch

from django.test import TestCase

from teatree.core.modelkit.phases import BACKLOG_SWEEP_PHASE
from teatree.core.models import Task, Ticket
from teatree.loops.backlog_sweep.loop import nudge_for_dream_gaps

UMBRELLA = "https://gitlab.com/o/f/-/work_items/249"


class TestNudgeForDreamGaps(TestCase):
    def setUp(self) -> None:
        Ticket.objects.create(issue_url=UMBRELLA, extra={"dream_gap_pending": [{"gap_key": "3a9f11"}]})
        patcher = patch("teatree.loop.global_scanner_factories.dream_umbrella_url", return_value=UMBRELLA)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_admitted_sweep_loop_queues_a_sweep_for_the_pending_gaps(self) -> None:
        with patch("teatree.loops.enable_verdict.loop_admits", return_value=True):
            assert nudge_for_dream_gaps() is True
        assert Task.objects.filter(phase=BACKLOG_SWEEP_PHASE).count() == 1

    def test_a_switched_off_sweep_loop_is_never_nudged(self) -> None:
        with patch("teatree.loops.enable_verdict.loop_admits", return_value=False):
            assert nudge_for_dream_gaps() is False
        assert not Task.objects.filter(phase=BACKLOG_SWEEP_PHASE).exists()
