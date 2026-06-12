"""Idle-pane reaper for the inert maker-only pane layer (#1838 PR#7a).

A sibling of the idle-stack reaper (``teatree.core.gates.idle_stack``): each scan
demotes any maker pane (a ``team:<role>`` claim) that has had NO live Session/Task
driving it for longer than ``teams_idle_minutes`` → STOPPED (the claim is
released so a future spawn can reuse the slot). Fail-safe: every uncertainty
resolves to KEEP. Inert — nothing in the live path imports it while the pane
layer ships dark.
"""

import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket
from teatree.teams.pane_reaper import reap_idle_panes, reapable_panes
from teatree.teams.panes import PaneState, TeammatePane
from teatree.teams.roles import TeamRole, team_claim_slot


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="", issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")


def _claimed_pane_task(*, idle_minutes_ago: float | None, end_session: bool) -> Task:
    """A task claimed under ``team:core-maker`` whose heartbeat is *idle_minutes_ago* old."""
    ticket = _ticket()
    session = Session.objects.create(ticket=ticket, agent_id="a")
    task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)
    TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
    if idle_minutes_ago is None:
        # A null heartbeat cannot be confirmed idle — the fail-safe KEEP branch.
        Task.objects.filter(pk=task.pk).update(heartbeat_at=None)
    else:
        stamp = timezone.now() - timedelta(minutes=idle_minutes_ago)
        Task.objects.filter(pk=task.pk).update(heartbeat_at=stamp, lease_expires_at=stamp)
    if end_session:
        Session.objects.filter(ticket=ticket).update(ended_at=timezone.now())
    task.refresh_from_db()
    return task


class TestReapablePanes(TestCase):
    def test_stale_pane_with_no_live_session_is_reapable(self) -> None:
        task = _claimed_pane_task(idle_minutes_ago=45, end_session=True)
        reapable = [t.pk for t in reapable_panes(idle_minutes=30)]
        assert task.pk in reapable

    def test_fresh_pane_is_not_reapable(self) -> None:
        task = _claimed_pane_task(idle_minutes_ago=5, end_session=True)
        reapable = [t.pk for t in reapable_panes(idle_minutes=30)]
        assert task.pk not in reapable

    def test_pane_with_live_session_is_not_reapable(self) -> None:
        task = _claimed_pane_task(idle_minutes_ago=45, end_session=False)
        reapable = [t.pk for t in reapable_panes(idle_minutes=30)]
        assert task.pk not in reapable

    def test_null_heartbeat_fails_safe_to_keep(self) -> None:
        task = _claimed_pane_task(idle_minutes_ago=None, end_session=True)
        reapable = [t.pk for t in reapable_panes(idle_minutes=30)]
        assert task.pk not in reapable

    def test_non_team_claim_is_ignored(self) -> None:
        ticket = _ticket()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)
        stamp = timezone.now() - timedelta(minutes=99)
        Task.objects.filter(pk=task.pk).update(
            status=Task.Status.CLAIMED, claimed_by="some-worker", heartbeat_at=stamp, lease_expires_at=stamp
        )
        Session.objects.filter(ticket=ticket).update(ended_at=timezone.now())
        reapable = [t.pk for t in reapable_panes(idle_minutes=30)]
        assert task.pk not in reapable


class TestReapIdlePanes(TestCase):
    def test_reap_demotes_a_stale_pane_to_stopped(self) -> None:
        task = _claimed_pane_task(idle_minutes_ago=45, end_session=True)
        count = reap_idle_panes(idle_minutes=30)
        assert count == 1

        task.refresh_from_db()
        assert task.claimed_by == ""
        assert task.status != Task.Status.CLAIMED
        pane = TeammatePane(task, role=TeamRole.CORE_MAKER)
        assert pane.refreshed_state() == PaneState.STOPPED

    def test_reap_keeps_a_fresh_pane(self) -> None:
        task = _claimed_pane_task(idle_minutes_ago=5, end_session=True)
        count = reap_idle_panes(idle_minutes=30)
        assert count == 0

        task.refresh_from_db()
        assert task.claimed_by == team_claim_slot(TeamRole.CORE_MAKER)

    def test_reap_keeps_a_pane_with_a_live_session(self) -> None:
        task = _claimed_pane_task(idle_minutes_ago=45, end_session=False)
        count = reap_idle_panes(idle_minutes=30)
        assert count == 0
        task.refresh_from_db()
        assert task.claimed_by == team_claim_slot(TeamRole.CORE_MAKER)
