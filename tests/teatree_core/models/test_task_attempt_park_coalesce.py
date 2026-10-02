"""A repeated, UNCHANGED usage-window park records STATE, not one audit row per poll.

The measured damage: 338,741 of 339,944 ``TaskAttempt`` rows were ``limit_parked:``
audit rows — 231,829 of them the single admission-guard reason repeated verbatim, and
47,172 on ONE task inside eight hours. A 1.2 GB control DB whose real dispatches were
1,203 rows. The park is a scheduling event on an unchanged condition, so an unchanged
reason must update the row it already wrote, never append another.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LIMIT_PARKED_PREFIX, Session, Task, TaskAttempt, Ticket

_ADMISSION = f"{LIMIT_PARKED_PREFIX}admission: all_accounts_exhausted window on lane 'subscription' active"
_ALL_SPENT = f"{LIMIT_PARKED_PREFIX}all configured subscription accounts exhausted — auto-resume at reset"


class ParkCoalescingTest(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket)
        cls.task = Task.objects.create(ticket=cls.ticket, session=cls.session)

    def _park(self, task: Task, reason: str) -> TaskAttempt:
        return TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=1,
            error=reason,
        )

    def test_an_unchanged_park_reason_updates_one_row(self) -> None:
        first = self._park(self.task, _ADMISSION)
        for _ in range(9):
            self._park(self.task, _ADMISSION)

        assert self.task.attempts.count() == 1
        first.refresh_from_db()
        assert first.park_repeats == 9

    def test_the_coalesced_row_is_returned_to_the_caller(self) -> None:
        """The park recorder returns the attempt it wrote; a repeat must not return None."""
        first = self._park(self.task, _ADMISSION)
        again = self._park(self.task, _ADMISSION)

        assert again.pk == first.pk
        assert again.park_repeats == 1

    def test_the_coalesced_row_carries_the_latest_end_time(self) -> None:
        first = self._park(self.task, _ADMISSION)
        original_end = first.ended_at
        again = self._park(self.task, _ADMISSION)

        assert original_end is not None
        assert again.ended_at is not None
        assert again.ended_at >= original_end

    def test_a_changed_park_reason_opens_a_new_row(self) -> None:
        """The condition changed, so the audit trail must show it."""
        self._park(self.task, _ADMISSION)
        self._park(self.task, _ALL_SPENT)

        assert self.task.attempts.count() == 2

    def test_a_park_after_real_work_opens_a_new_row(self) -> None:
        """A park that follows an actual dispatch is a fresh episode, not a repeat."""
        self._park(self.task, _ADMISSION)
        TaskAttempt.objects.create(task=self.task, exit_code=0)
        self._park(self.task, _ADMISSION)

        assert self.task.attempts.filter(error=_ADMISSION).count() == 2

    def test_parks_on_different_tasks_never_coalesce(self) -> None:
        other = Task.objects.create(ticket=self.ticket, session=self.session)
        self._park(self.task, _ADMISSION)
        self._park(other, _ADMISSION)

        assert TaskAttempt.objects.count() == 2

    def test_a_repeated_real_failure_still_appends(self) -> None:
        """Only park rows coalesce — a repeated genuine failure is real work, one row each."""
        for _ in range(3):
            TaskAttempt.objects.create(
                task=self.task,
                exit_code=1,
                error="Traceback (most recent call last): boom",
            )

        assert self.task.attempts.count() == 3

    def test_a_coalesced_park_is_not_counted_as_extra_work(self) -> None:
        """``iteration`` stays at the park sentinel however many polls fold into the row."""
        first = self._park(self.task, _ADMISSION)
        self._park(self.task, _ADMISSION)

        first.refresh_from_db()
        assert first.iteration == 0
