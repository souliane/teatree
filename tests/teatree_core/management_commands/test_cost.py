"""Tests for the ``t3 cost`` management command (SDK-equivalent spend)."""

import json
from datetime import datetime
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt
from tests.factories import TicketFactory

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str, **kwargs: object) -> str:
    buf = StringIO()
    call_command("cost", *args, stdout=buf, **kwargs)
    return buf.getvalue()


class TestCostCommand:
    def setup_method(self) -> None:
        self.ticket = TicketFactory()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def _attempt(
        self,
        *,
        cost: float | None,
        when: datetime,
        target: str = Task.ExecutionTarget.HEADLESS,
        **fields: object,
    ) -> TaskAttempt:
        attempt = TaskAttempt.objects.create(task=self.task, execution_target=target, cost_usd=cost, **fields)
        # ``started_at`` is auto_now_add; an update() bypasses it to place the
        # row inside or outside the billing cycle under test.
        TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=when)
        return attempt

    def test_sums_headless_cost_in_current_cycle(self) -> None:
        now = timezone.now()
        self._attempt(cost=3.0, when=now)
        self._attempt(cost=1.4, when=now)
        out = _call(json_output=True)
        payload = json.loads(out)
        assert payload["cycle_to_date_usd"] == pytest.approx(4.4)
        assert payload["attempts"] == 2
        assert payload["credit_usd"] == pytest.approx(200.0)
        assert payload["chip"] == "SDK mtd ≈$4/$200"

    def test_excludes_interactive_attempts(self) -> None:
        now = timezone.now()
        self._attempt(cost=10.0, when=now, target=Task.ExecutionTarget.INTERACTIVE)
        self._attempt(cost=2.0, when=now)
        payload = json.loads(_call(json_output=True))
        assert payload["cycle_to_date_usd"] == pytest.approx(2.0)
        assert payload["attempts"] == 1

    def test_excludes_attempts_before_cycle_start(self) -> None:
        now = timezone.now()
        last_cycle = now.replace(year=now.year - 1)
        self._attempt(cost=99.0, when=last_cycle)
        self._attempt(cost=2.0, when=now)
        payload = json.loads(_call(json_output=True))
        assert payload["cycle_to_date_usd"] == pytest.approx(2.0)

    def test_per_model_breakdown(self) -> None:
        now = timezone.now()
        self._attempt(cost=4.0, when=now, model="claude-opus-4-8")
        self._attempt(cost=1.0, when=now, model="claude-sonnet-4-6")
        payload = json.loads(_call(json_output=True))
        assert payload["per_model_usd"]["opus"] == pytest.approx(4.0)
        assert payload["per_model_usd"]["sonnet"] == pytest.approx(1.0)

    def test_human_output_shows_credit_and_projection(self) -> None:
        self._attempt(cost=20.0, when=timezone.now())
        out = _call()
        assert "cycle-to-date:" in out
        assert "$200 credit" in out
        assert "projected end-of-cycle:" in out
