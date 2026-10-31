"""A headless run killed by a rate-limit error parks and is claimable again once its account is usable."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.agents.runner import run_agent
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import assistant_text, fake_sdk, result_message


class TestKilledRunResumes(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = planned_ticket()

    def _killed_by_rate_limit(self) -> Task:
        killed = result_message(
            subtype="error_during_execution",
            is_error=True,
            result="You've hit your weekly limit. It resets in 3 hours.",
        )
        with fake_sdk([assistant_text("starting"), killed]):
            task = Task.objects.create(ticket=self.ticket, session=Session.objects.create(ticket=self.ticket))
            run_agent(task, phase="coding", overlay_skill_metadata={})
        task.refresh_from_db()
        return task

    def test_a_rate_limit_kill_parks_the_task_instead_of_failing_it(self) -> None:
        task = self._killed_by_rate_limit()
        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert task.not_before > timezone.now()

    def test_the_parked_task_is_not_claimable_while_the_account_is_exhausted(self) -> None:
        task = self._killed_by_rate_limit()
        assert not Task.objects.claimable().filter(pk=task.pk).exists()

    def test_the_parked_task_is_claimable_once_the_account_is_usable(self) -> None:
        task = self._killed_by_rate_limit()
        assert task.not_before is not None
        with patch("teatree.core.managers.timezone.now", return_value=task.not_before + timedelta(seconds=1)):
            assert Task.objects.claimable().filter(pk=task.pk).exists()
