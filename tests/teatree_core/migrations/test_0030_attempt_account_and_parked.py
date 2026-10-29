from importlib import import_module

from django.apps import apps
from django.test import TestCase

from teatree.core.models import Session, Task, TaskAttempt
from tests.factories import planned_ticket


class TestRestampParkedAttempts(TestCase):
    def test_only_existing_park_rows_are_restamped(self) -> None:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        parked = TaskAttempt.objects.create(task=task, exit_code=1, error="limit_parked: reset")
        failed = TaskAttempt.objects.create(task=task, exit_code=1, error="harness_crash: failed")
        TaskAttempt.objects.filter(pk=parked.pk).update(outcome=TaskAttempt.Outcome.CRASH)
        restamp = import_module("teatree.core.migrations.0030_attempt_account_and_parked").restamp_parked_attempts

        restamp(apps, None)
        parked.refresh_from_db()
        failed.refresh_from_db()

        assert parked.outcome == TaskAttempt.Outcome.PARKED
        assert failed.outcome == TaskAttempt.Outcome.CRASH
