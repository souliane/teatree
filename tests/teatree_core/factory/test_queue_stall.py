"""The shared queue-stall predicate the doctor check and the dispatch-gap detector both read."""

from datetime import timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.agents.runner_skill_staging import staged_skills_or_refusal
from teatree.core.factory.factory_signal_queries import S5Evidence, compute_s5, current_window
from teatree.core.factory.queue_stall import DEFAULT_STALL_MINUTES, read_queue_stall, stall_minutes
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.task_repair import phase_attempts
from teatree.skill_support import index as skill_index
from tests._unreadable_apm_manifest import unreadable_running_manifest


class StallMinutesTests(TestCase):
    def test_default(self) -> None:
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("TEATREE_QUEUE_STALL_MINUTES", None)
            assert stall_minutes() == DEFAULT_STALL_MINUTES

    def test_override_within_bounds(self) -> None:
        with patch.dict("os.environ", {"TEATREE_QUEUE_STALL_MINUTES": "12"}):
            assert stall_minutes() == 12

    def test_invalid_or_out_of_range_override_falls_back(self) -> None:
        for raw in ("soon", "0", str(24 * 60 + 1)):
            with patch.dict("os.environ", {"TEATREE_QUEUE_STALL_MINUTES": raw}):
                assert stall_minutes() == DEFAULT_STALL_MINUTES


class ReadQueueStallTests(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="acme")
        self.session = Session.objects.create(ticket=self.ticket)

    def _task(self, age_minutes: int) -> Task:
        task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(minutes=age_minutes))
        return task

    def test_names_the_oldest_row_and_counts_the_set(self) -> None:
        oldest = self._task(age_minutes=50)
        self._task(age_minutes=40)

        stall = read_queue_stall(Task.objects.filter(status=Task.Status.PENDING), now=timezone.now(), minutes=30)

        assert stall is not None
        assert stall.pending == 2
        assert stall.oldest_pk == oldest.pk

    def test_empty_set_is_no_stall(self) -> None:
        assert read_queue_stall(Task.objects.none(), now=timezone.now(), minutes=30) is None


class UnreadablePinsParkIsNotProgressTests(TestCase):
    def test_a_queue_parked_on_unreadable_pins_past_the_window_is_a_stall_and_burns_no_budget(self) -> None:
        ticket = Ticket.objects.create(overlay="acme")
        session = Session.objects.create(ticket=ticket)
        tasks = [Task.objects.create(ticket=ticket, session=session, phase="reviewing") for _ in range(2)]
        Task.objects.filter(ticket=ticket).update(created_at=timezone.now() - timedelta(minutes=50))
        local = Path.home() / "repo-skills"
        (local / "rules").mkdir(parents=True)
        (local / "rules" / "SKILL.md").write_text("---\nname: rules\n---\n", encoding="utf-8")

        with unreadable_running_manifest(Path.home()), patch.object(skill_index, "DEFAULT_SKILLS_DIR", local):
            for task in tasks:
                staged_skills_or_refusal(task, phase="reviewing", overlay_skill_metadata={})

        now = timezone.now()
        stall = read_queue_stall(Task.objects.filter(status=Task.Status.PENDING), now=now, minutes=30)
        evidence = cast("S5Evidence", compute_s5(current_window(now + timedelta(seconds=1), 7), "acme", now).evidence)
        assert stall is not None
        assert stall.pending == 2
        assert all(phase_attempts(task) == [] for task in tasks)
        assert (evidence["attempts"], evidence["in_flight"]) == (0, 0)
