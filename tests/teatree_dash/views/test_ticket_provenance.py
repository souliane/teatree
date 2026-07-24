"""Per-task provenance panel on the ticket drawer (#3673 Tier 1).

Tier 1 is display-only over ``TaskAttempt``'s already-recorded fields — no
migration. The panel must render model / duration / cost / tokens / lane /
outcome, make the estimated-vs-reported cost distinction visually explicit, and
stay query-bounded (never scale per attempt — #3674).
"""

from datetime import timedelta
from typing import cast

import pytest
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.dash.ticket_detail import build_ticket_detail
from tests.factories import TaskAttemptFactory, TaskFactory, TicketFactory

State = Ticket.State


def _attempt(task, **kwargs) -> TaskAttempt:
    attempt = cast("TaskAttempt", TaskAttemptFactory(task=task, **kwargs))
    # started_at is auto_now_add; set an ended_at so duration is derivable.
    TaskAttempt.objects.filter(pk=attempt.pk).update(ended_at=attempt.started_at + timedelta(seconds=95))
    attempt.refresh_from_db()
    return attempt


class ProvenanceReadModelTestCase(TestCase):
    def test_attempt_provenance_fields_are_exposed(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        _attempt(
            task,
            model="claude-opus-4-8",
            cost_usd=0.4231,
            cost_is_estimated=False,
            input_tokens=1200,
            output_tokens=340,
            cache_read_tokens=5000,
            cache_write_tokens=80,
            num_turns=7,
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            agent_session_id="sess-abc",
            launch_url="https://example.test/run/1",
            artifact_path="/tmp/artifact.json",
        )
        detail = build_ticket_detail(ticket.pk)
        row = detail.tasks[0].attempts[0]
        assert row.model == "claude-opus-4-8"
        assert row.duration == "1m 35s"
        assert row.cost_usd == pytest.approx(0.4231)
        assert row.cost_is_estimated is False
        assert row.input_tokens == 1200
        assert row.output_tokens == 340
        assert row.cache_read_tokens == 5000
        assert row.cache_write_tokens == 80
        assert row.num_turns == 7
        assert row.lane == TaskAttempt.Lane.SUBSCRIPTION
        assert row.agent_session_id == "sess-abc"
        assert row.launch_url == "https://example.test/run/1"
        assert row.artifact_path == "/tmp/artifact.json"

    def test_tier3_effort_and_skills_are_exposed(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        _attempt(task, model="m", reasoning_effort="xhigh", skills_loaded=["t3:code", "t3:rules"])
        row = build_ticket_detail(ticket.pk).tasks[0].attempts[0]
        assert row.reasoning_effort == "xhigh"
        assert row.skills_loaded == ("t3:code", "t3:rules")

    def test_tier3_fields_default_empty_for_a_legacy_attempt(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        _attempt(task, model="m")  # no effort/skills recorded (a pre-#3673 row)
        row = build_ticket_detail(ticket.pk).tasks[0].attempts[0]
        assert row.reasoning_effort == ""
        assert row.skills_loaded == ()

    def test_running_attempt_has_blank_duration(self) -> None:
        # A still-running attempt has no ended_at, so its duration renders blank
        # (the elapsed span is not derivable yet) rather than crashing the drawer.
        ticket = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        running = cast("TaskAttempt", TaskAttemptFactory(task=task, model="m"))
        assert running.ended_at is None  # the factory leaves a live attempt open
        detail = build_ticket_detail(ticket.pk)
        assert detail.tasks[0].attempts[0].duration == ""

    def test_query_count_does_not_scale_with_attempt_count(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        one_task = TaskFactory(ticket=ticket, phase="coding")
        _attempt(one_task, model="m")
        with CaptureQueriesContext(connection) as small:
            build_ticket_detail(ticket.pk)

        big = TicketFactory(state=State.STARTED)
        for _ in range(6):
            tk = TaskFactory(ticket=big, phase="coding")
            for _ in range(5):
                _attempt(tk, model="m")
        with CaptureQueriesContext(connection) as large:
            build_ticket_detail(big.pk)

        # A bounded plan: 6 tasks x 5 attempts must not cost more queries than 1 x 1.
        assert len(large) <= len(small), f"drawer N+1: {len(small)} -> {len(large)}"


class ProvenanceDrawerRenderTestCase(TestCase):
    def test_drawer_renders_provenance_and_marks_estimated_cost(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        _attempt(task, model="claude-sonnet", cost_usd=0.12, cost_is_estimated=True, num_turns=3)
        body = self.client.get(reverse("dash:ticket_drawer", args=[ticket.pk])).content.decode()
        assert "claude-sonnet" in body
        # estimated cost must be visually distinct — an "est" marker, never a bare number.
        assert "est" in body.lower()

    def test_reported_cost_is_marked_reported_not_estimated(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        _attempt(task, model="claude-sonnet", cost_usd=0.9, cost_is_estimated=False)
        body = self.client.get(reverse("dash:ticket_drawer", args=[ticket.pk])).content.decode()
        assert "reported" in body.lower()

    def test_drawer_renders_effort_and_skill_chips(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        _attempt(task, model="m", reasoning_effort="xhigh", skills_loaded=["t3:code", "t3:rules"])
        body = self.client.get(reverse("dash:ticket_drawer", args=[ticket.pk])).content.decode()
        assert "xhigh" in body
        assert "t3:code" in body
        assert "t3:rules" in body
