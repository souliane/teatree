"""``TaskAttempt.Lane`` attribution and the per-row ``effective_tokens`` metric."""

import pytest
from django.test import TestCase

from teatree.core.models import Session, Task, TaskAttempt, Ticket


class TestTaskAttemptEffectiveTokens(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket)
        cls.task = Task.objects.create(ticket=cls.ticket, session=cls.session)

    def test_none_when_no_tokens_captured(self) -> None:
        attempt = TaskAttempt.objects.create(task=self.task)
        assert attempt.effective_tokens is None

    def test_computed_from_captured_tokens(self) -> None:
        attempt = TaskAttempt.objects.create(
            task=self.task,
            model="claude-opus-4-8",
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=2000,
        )
        # opus (m=1.0): 1000 + 0.1*2000 + 4*100 = 1600.
        assert attempt.effective_tokens == pytest.approx(1600.0)

    def test_zero_tokens_still_computes_zero_not_none(self) -> None:
        # An explicit 0 differs from "never captured" — a real, billed
        # zero-token attempt reports 0.0, not None.
        attempt = TaskAttempt.objects.create(
            task=self.task,
            model="opus",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
        )
        assert attempt.effective_tokens == pytest.approx(0.0)


class TestTaskAttemptLane(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()
        cls.session = Session.objects.create(ticket=cls.ticket)
        cls.task = Task.objects.create(ticket=cls.ticket, session=cls.session)

    def test_defaults_to_blank(self) -> None:
        attempt = TaskAttempt.objects.create(task=self.task)
        assert attempt.lane == ""

    def test_stores_subscription_and_metered_choices(self) -> None:
        subscription = TaskAttempt.objects.create(task=self.task, lane=TaskAttempt.Lane.SUBSCRIPTION)
        metered = TaskAttempt.objects.create(task=self.task, lane=TaskAttempt.Lane.METERED)
        subscription.refresh_from_db()
        metered.refresh_from_db()
        assert subscription.lane == "subscription"
        assert metered.lane == "metered"


class TestTaskAttemptQuerySetUsagesCarriesLane(TestCase):
    def test_usages_carries_lane_through_to_attempt_usage(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(
            task=task,
            model="opus",
            input_tokens=100,
            lane=TaskAttempt.Lane.METERED,
        )
        [usage] = TaskAttempt.objects.usages()
        assert usage.lane == "metered"
