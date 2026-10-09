"""``DispatchGapDetector`` — claimable work nobody has claimed or run for a whole window."""

import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.mode_resolution import set_mode_override
from teatree.core.models import Mode, SelfImproveFiring, Session, Task, TaskAttempt, Ticket
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
from teatree.loop.self_improve.actions import run_action_ladder
from teatree.loop.self_improve.detectors import DispatchGapDetector


class DispatchGapDetectorTests(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/1")
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent")

    def _task(self, *, age_minutes: int, status: str = Task.Status.PENDING) -> Task:
        task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding", status=status)
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(minutes=age_minutes))
        return task

    def test_fires_on_a_stalled_claimable_queue(self) -> None:
        self._task(age_minutes=45)

        reports = DispatchGapDetector().detect()

        assert len(reports) == 1
        assert reports[0].severity == "warn"
        assert reports[0].payload["pending_count"] == 1
        assert reports[0].payload["oldest_age_minutes"] >= 45

    def test_a_consolidation_registry_holder_no_longer_silences_it(self) -> None:
        self._task(age_minutes=45)
        with TemporaryDirectory() as registry_dir:
            (Path(registry_dir) / "consolidation-registry.json").write_text(
                json.dumps({"agent-a": {"session_id": "s1", "pid": 1}}), encoding="utf-8"
            )
            with patch.dict("os.environ", {"T3_LOOP_REGISTRY_DIR": registry_dir}):
                reports = DispatchGapDetector().detect()

        assert len(reports) == 1

    def test_quiet_when_a_row_is_claimed(self) -> None:
        self._task(age_minutes=45)
        self._task(age_minutes=46, status=Task.Status.CLAIMED)

        assert DispatchGapDetector().detect() == []

    def test_quiet_after_a_recent_real_attempt(self) -> None:
        self._task(age_minutes=45)
        done = self._task(age_minutes=50, status=Task.Status.COMPLETED)
        TaskAttempt.objects.create(task=done)

        assert DispatchGapDetector().detect() == []

    def test_a_recent_limit_park_attempt_is_not_progress(self) -> None:
        self._task(age_minutes=45)
        parked = self._task(age_minutes=50, status=Task.Status.FAILED)
        TaskAttempt.objects.create(task=parked, error=f"{LIMIT_PARKED_PREFIX} weekly window")

        assert len(DispatchGapDetector().detect()) == 1

    def test_quiet_while_the_oldest_row_is_inside_the_window(self) -> None:
        self._task(age_minutes=5)

        assert DispatchGapDetector().detect() == []

    def test_quiet_while_a_lifted_park_has_not_yet_waited_a_window(self) -> None:
        task = self._task(age_minutes=120)
        Task.objects.filter(pk=task.pk).update(not_before=timezone.now() - timedelta(minutes=5))

        assert DispatchGapDetector().detect() == []

    def test_fires_once_a_lifted_park_has_waited_a_window(self) -> None:
        task = self._task(age_minutes=120)
        Task.objects.filter(pk=task.pk).update(not_before=timezone.now() - timedelta(minutes=45))

        assert len(DispatchGapDetector().detect()) == 1

    def test_quiet_while_a_future_park_holds_the_row(self) -> None:
        task = self._task(age_minutes=120)
        Task.objects.filter(pk=task.pk).update(not_before=timezone.now() + timedelta(hours=1))

        assert DispatchGapDetector().detect() == []

    def test_pending_count_excludes_a_parked_row(self) -> None:
        parked = self._task(age_minutes=120)
        Task.objects.filter(pk=parked.pk).update(not_before=timezone.now() + timedelta(hours=1))
        self._task(age_minutes=45)

        reports = DispatchGapDetector().detect()

        assert len(reports) == 1
        assert reports[0].payload["pending_count"] == 1

    def test_quiet_while_the_active_mode_masks_dispatch(self) -> None:
        self._task(age_minutes=45)
        Mode.objects.create(name="dispatch-off-test", entries={"dispatch": False})
        set_mode_override("dispatch-off-test", reason="test")

        assert DispatchGapDetector().detect() == []

    def test_state_hash_is_stable_while_the_same_row_stays_stuck(self) -> None:
        self._task(age_minutes=45)

        first = DispatchGapDetector().detect()
        second = DispatchGapDetector().detect()

        assert first[0].state_hash == second[0].state_hash

    def test_repeated_cycles_on_one_stuck_row_record_one_firing(self) -> None:
        self._task(age_minutes=45)

        for _ in range(2):
            for report in DispatchGapDetector().detect():
                run_action_ladder(report)

        firing = SelfImproveFiring.objects.get(detector="dispatch_gap")
        assert firing.action_count == 1

    def test_action_ladder_ceiling_is_ticket(self) -> None:
        self._task(age_minutes=45)

        reports = DispatchGapDetector().detect()

        assert reports[0].max_rung == SelfImproveFiring.Action.TICKET.value

    def test_auto_fix_false(self) -> None:
        assert DispatchGapDetector.auto_fix is False
