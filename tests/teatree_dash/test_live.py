"""The live-work view: what the factory is executing right now (#3856, #3886).

The board renders ticket FSM state, which is a lagging view — it answers "where did
work get to", never "is anything running at this moment". Five pages existed and none
of them answered the question an operator asks most often about an autonomous system,
so the only way to learn what was in flight was to open the database by hand.

Every panel here is a rendering of state that is ALREADY recorded: a running attempt
is a ``TaskAttempt`` with no ``ended_at``, the queue is ``Task.status``, a loop's
refusal is the very string the live tick's admission gate produces, and the skill
bundle is the ``skills_loaded`` the dispatch persisted. Nothing is re-derived at
render time — a re-derivation would report today's answer for yesterday's dispatch,
which is exactly the fault the skills panel exists to expose (#3886).
"""

# test-path: cross-cutting — one page over attempts, tasks and the loop registry

import datetime as dt
import os
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from teatree.core.models.loop import Loop
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.dash.live import LIVE_OUTCOME_ROWS, build_live_view
from teatree.dash.views.base import NAV_ITEMS
from teatree.loops.loop_table import loop_block_reasons
from teatree.loops.registry import iter_loops
from tests.factories import TaskFactory, TicketFactory

State = Ticket.State
_SECRET = "hunter2-live-view-token"
_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
#: Patched where it is DEFINED — the read model reaches it through a deferred import.
_STARVED_SEAM = "teatree.loops.chain_membership.starved_loop_names"


def _running(**kwargs: object) -> TaskAttempt:
    """An attempt that has started and not ended — the definition of 'running'."""
    ticket = TicketFactory(state=State.STARTED, short_description="live subject")
    task = TaskFactory(ticket=ticket, phase="coding")
    defaults = {"ended_at": None}
    return TaskAttempt.objects.create(task=task, **{**defaults, **kwargs})


class RunningWorkIsVisibleTestCase(TestCase):
    def test_the_nav_offers_the_live_page(self) -> None:
        assert ("dash:live", "Live") in NAV_ITEMS

    def test_an_unfinished_attempt_is_listed_as_running(self) -> None:
        attempt = _running(model="claude-opus-4-8", lane=TaskAttempt.Lane.SUBSCRIPTION)
        row = build_live_view().running[0]
        assert row.phase == "coding"
        assert row.short_description == "live subject"
        assert row.model == "claude-opus-4-8"
        assert row.elapsed, "a running attempt must report how long it has been going"
        assert row.attempt_id == attempt.pk

    def test_a_finished_attempt_is_not_running(self) -> None:
        _running(ended_at=timezone.now())
        assert build_live_view().running == ()

    def test_the_page_names_the_running_ticket(self) -> None:
        _running()
        body = self.client.get(reverse("dash:live"), **_LOOPBACK).content.decode()
        assert "live subject" in body


class QueueDepthIsVisibleTestCase(TestCase):
    def test_pending_and_claimed_are_counted_apart(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        TaskFactory(ticket=ticket, phase="coding", status=Task.Status.PENDING)
        TaskFactory(ticket=ticket, phase="testing", status=Task.Status.PENDING)
        TaskFactory(ticket=ticket, phase="reviewing", status=Task.Status.CLAIMED)
        view = build_live_view()
        assert view.pending_count == 2
        assert view.claimed_count == 1

    def test_a_terminal_task_is_in_neither_count(self) -> None:
        TaskFactory(ticket=TicketFactory(state=State.STARTED), phase="coding", status=Task.Status.COMPLETED)
        view = build_live_view()
        assert (view.pending_count, view.claimed_count) == (0, 0)

    def test_the_queue_names_what_is_next(self) -> None:
        TaskFactory(ticket=TicketFactory(state=State.STARTED), phase="shipping", status=Task.Status.PENDING)
        assert [row.phase for row in build_live_view().queued] == ["shipping"]


class LoopLivenessStatesWhyALoopDidNotRunTestCase(TestCase):
    """The refusal reason is the tick's own, not a second vocabulary invented here."""

    def test_a_loop_with_no_seeded_row_reports_the_ticks_own_reason(self) -> None:
        name = iter_loops()[0].name
        Loop.objects.filter(name=name).delete()
        row = next(row for row in build_live_view().loops if row.name == name)
        assert row.blocked_reason == "no Loop row — this loop's config was never seeded"
        assert not row.dispatching

    def test_every_rendered_reason_is_the_admission_gates_own_string(self) -> None:
        rendered = {row.name: row.blocked_reason for row in build_live_view().loops}
        assert rendered == loop_block_reasons(timezone.now())

    def test_a_due_enabled_loop_reports_no_refusal(self) -> None:
        name = iter_loops()[0].name
        Loop.objects.filter(name=name).delete()
        Loop.objects.create(name=name, script=f"{name}/run.py", delay_seconds=60, enabled=True, last_run_at=None)
        row = next(row for row in build_live_view().loops if row.name == name)
        assert row.blocked_reason == ""
        assert row.dispatching


class RecentOutcomesTailTestCase(TestCase):
    def test_a_finished_attempt_appears_in_the_tail(self) -> None:
        # ``outcome`` is derived from exit_code + error on save, never set directly.
        _running(ended_at=timezone.now(), exit_code=0)
        assert [row.outcome for row in build_live_view().outcomes] == ["Success"]

    def test_the_tail_is_bounded(self) -> None:
        ticket = TicketFactory(state=State.STARTED)
        task = TaskFactory(ticket=ticket, phase="coding")
        now = timezone.now()
        TaskAttempt.objects.bulk_create(
            TaskAttempt(task=task, ended_at=now, exit_code=0) for _ in range(LIVE_OUTCOME_ROWS + 5)
        )
        assert len(build_live_view().outcomes) == LIVE_OUTCOME_ROWS

    def test_a_park_is_marked_as_one_so_a_park_storm_is_visible(self) -> None:
        _running(ended_at=timezone.now(), error="limit_parked: usage window exhausted", park_repeats=12)
        row = build_live_view().outcomes[0]
        assert row.parked
        assert row.park_repeats == 12


class SkillBundleIsVisiblePerTaskTestCase(TestCase):
    """#3886 — the resolved bundle the dispatch RAN with, and an empty one as a fault."""

    def test_a_running_row_carries_the_bundle_the_dispatch_recorded(self) -> None:
        _running(skills_loaded=["t3:code", "t3:rules"])
        row = build_live_view().running[0]
        assert row.skills == ("t3:code", "t3:rules")
        assert not row.skills_fault

    def test_a_agent_runner_that_recorded_no_bundle_reads_as_a_fault(self) -> None:
        _running(skills_loaded=[])
        assert build_live_view().running[0].skills_fault

    def test_the_page_states_the_fault_rather_than_rendering_blank(self) -> None:
        _running(skills_loaded=[])
        body = self.client.get(reverse("dash:live"), **_LOOPBACK).content.decode()
        assert "no skills recorded" in body

    def test_the_page_shows_each_running_tasks_skills(self) -> None:
        _running(skills_loaded=["t3:code"])
        body = self.client.get(reverse("dash:live"), **_LOOPBACK).content.decode()
        assert "t3:code" in body


class LiveViewIsPolledSafelyTestCase(TestCase):
    def test_the_body_fragment_answers_on_its_own(self) -> None:
        _running()
        assert self.client.get(reverse("dash:live_body"), **_LOOPBACK).status_code == 200

    def test_the_shell_keeps_its_heading_skip_link_and_labelled_nav(self) -> None:
        body = self.client.get(reverse("dash:live"), **_LOOPBACK).content.decode()
        assert "<h1" in body
        assert 'class="skip-link"' in body
        assert 'aria-label="Dashboard sections"' in body


class NoConfiguredSecretReachesTheLiveResponseTestCase(TestCase):
    """The live page renders dispatch facts, never an attempt's free-text error body."""

    def setUp(self) -> None:
        env = patch.dict(os.environ, {"T3_BANNED_TERMS": _SECRET})
        env.start()
        self.addCleanup(env.stop)

    def test_a_running_attempts_error_body_is_not_echoed(self) -> None:
        _running(error=f"auth failed with {_SECRET}")
        assert _SECRET not in self.client.get(reverse("dash:live"), **_LOOPBACK).content.decode()

    def test_a_finished_attempts_error_body_is_not_echoed(self) -> None:
        _running(ended_at=timezone.now(), error=f"crashed carrying {_SECRET}")
        assert _SECRET not in self.client.get(reverse("dash:live"), **_LOOPBACK).content.decode()


class LiveViewIsBoundedTestCase(TestCase):
    def test_the_loop_panel_lists_the_registry_not_every_stray_loop_row(self) -> None:
        Loop.objects.create(name="not-a-registered-mini-loop", script="run.py", delay_seconds=60)
        names = {row.name for row in build_live_view().loops}
        assert "not-a-registered-mini-loop" not in names
        assert names == {loop.name for loop in iter_loops()}

    def test_the_generated_at_stamp_is_present_so_a_frozen_page_is_obvious(self) -> None:
        assert isinstance(build_live_view().generated_at, dt.datetime)


class LoopStarvationIsItsOwnAxisTestCase(TestCase):
    """Admitted with no driver renders as ``starved``, not as a healthy row (#4185).

    Deliberately separate from ``blocked_reason``: that explains a REFUSAL, and a starved
    loop is genuinely admitted — the tick would dispatch it if anything ever fired.
    """

    @staticmethod
    def _admitted_loop() -> str:
        name = iter_loops()[0].name
        Loop.objects.filter(name=name).delete()
        Loop.objects.create(name=name, script=f"{name}/run.py", delay_seconds=60, enabled=True, last_run_at=None)
        return name

    def test_a_driverless_admitted_loop_is_starved(self) -> None:
        name = self._admitted_loop()
        with patch(_STARVED_SEAM, return_value={name}):
            row = next(row for row in build_live_view().loops if row.name == name)
        assert row.starved
        # Still admitted — starvation is a second axis, never a block reason.
        assert row.blocked_reason == ""

    def test_a_driven_admitted_loop_is_not_starved(self) -> None:
        name = self._admitted_loop()
        with patch(_STARVED_SEAM, return_value=set()):
            row = next(row for row in build_live_view().loops if row.name == name)
        assert not row.starved

    def test_the_loops_panel_renders_a_starved_chip(self) -> None:
        name = self._admitted_loop()
        with patch(_STARVED_SEAM, return_value={name}):
            response = self.client.get(reverse("dash:live"), **_LOOPBACK)
        assert "starved" in response.content.decode()
