"""The compact SDK-equivalent cost chip on the statusline (#1714).

The chip carries an explicit ``mtd`` (month-to-date) period label and is handed
to the statusline header as the first ``usage``-placed contributed segment in
``tick-meta.json``'s ``segments`` list (#3237), which ``hooks/scripts/statusline.sh``
renders next to the weekly (``7d=``) rate-limit segment.
"""

import datetime as dt
import json
from pathlib import Path

import pytest
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt
from teatree.loop.rendering import cost_chip_lines
from teatree.loop.tick_freshness import _write_tick_meta
from tests.factories import TicketFactory

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _headless_attempt(task: Task, *, cost: float) -> TaskAttempt:
    return TaskAttempt.objects.create(
        task=task,
        execution_target=Task.ExecutionTarget.HEADLESS,
        cost_usd=cost,
        started_at=timezone.now(),
    )


class TestCostChipLines:
    def setup_method(self) -> None:
        self.ticket = TicketFactory()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_silenced_when_no_headless_cost(self) -> None:
        assert cost_chip_lines() == []

    def test_renders_compact_chip(self) -> None:
        _headless_attempt(self.task, cost=30.0)
        _headless_attempt(self.task, cost=18.0)
        assert cost_chip_lines() == ["SDK mtd ≈$48/$200"]

    def test_chip_stays_tiny_at_high_spend(self) -> None:
        _headless_attempt(self.task, cost=1234.0)
        assert cost_chip_lines() == ["SDK mtd ≈$1234/$200"]

    def test_excludes_interactive_attempts(self) -> None:
        TaskAttempt.objects.create(
            task=self.task,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            cost_usd=500.0,
            started_at=timezone.now(),
        )
        assert cost_chip_lines() == []


class TestCostChipInTickMeta:
    def setup_method(self) -> None:
        self.ticket = TicketFactory()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def _meta(self, tmp_path: Path) -> dict:
        statusline = tmp_path / "statusline.txt"
        _write_tick_meta(dt.datetime(2026, 6, 10, tzinfo=dt.UTC), target=statusline)
        return json.loads((tmp_path / "tick-meta.json").read_text(encoding="utf-8"))

    def test_meta_carries_period_labelled_chip(self, tmp_path: Path) -> None:
        _headless_attempt(self.task, cost=48.0)
        # The cost chip is now the first ``usage``-placed contributed segment (#3237).
        chip = self._meta(tmp_path)["segments"][0]
        assert chip == {"id": "cost_chip", "text": "SDK mtd ≈$48/$200", "placement": "usage"}

    def test_meta_chip_empty_when_no_headless_cost(self, tmp_path: Path) -> None:
        assert all(s["id"] != "cost_chip" for s in self._meta(tmp_path)["segments"])

    def test_meta_carries_rendered_at_for_staleness_gate(self, tmp_path: Path) -> None:
        # The statusline freshness gate reads rendered_at to surface a STALE
        # banner on a frozen render — it must equal the tick's start epoch.
        started = dt.datetime(2026, 6, 10, tzinfo=dt.UTC)
        statusline = tmp_path / "statusline.txt"
        _write_tick_meta(started, target=statusline)
        meta = json.loads((tmp_path / "tick-meta.json").read_text(encoding="utf-8"))
        assert meta["rendered_at"] == int(started.timestamp())
