"""Pane lifecycle FSM over the existing Task + lease (#1838 PR#7a).

A maker pane is a long-lived ``team:<role>`` claim of a ``Task`` with a derived
lifecycle state — ``spawn → active → idle → stopped``. NO new model and NO
migration: the state is computed from the existing ``Task.status`` + lease
liveness + the live ``Session`` on the ticket, and transitions are the existing
claim / renew / clear primitives. The FSM is inert — nothing in the live path
imports it while the pane layer ships dark.
"""

import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket
from teatree.teams.panes import PaneState, TeammatePane
from teatree.teams.roles import TeamRole, team_claim_slot


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="", issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")


def _pending_task(ticket: Ticket) -> Task:
    session = Session.objects.create(ticket=ticket, agent_id="a")
    return Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)


class TestPaneSpawnAndState(TestCase):
    def test_spawn_claims_the_team_role_slot(self) -> None:
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)

        task.refresh_from_db()
        assert task.claimed_by == team_claim_slot(TeamRole.CORE_MAKER)
        assert task.status == Task.Status.CLAIMED
        assert pane.state == PaneState.ACTIVE

    def test_spawn_rejects_a_non_team_slot_collision(self) -> None:
        # The pane spawn path runs the claim-namespace guard — it can never
        # claim a loop-owner slot. REVIEWER carries a real ``team:<role>`` key,
        # so the guard passes; the guard rejection is exercised in
        # test_guardrails. Here we assert the spawned claim is always a team slot.
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.OVERLAY_MAKER)
        assert pane.claim_slot.startswith("team:")

    def test_active_pane_with_live_lease_and_session(self) -> None:
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
        assert pane.state == PaneState.ACTIVE

    def test_idle_when_lease_held_but_no_live_session(self) -> None:
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
        # End the only session on the ticket — the lease is still held but no
        # live Session/Task is driving the pane → IDLE.
        Session.objects.filter(ticket=ticket).update(ended_at=timezone.now())
        assert pane.refreshed_state() == PaneState.IDLE

    def test_idle_when_lease_expired(self) -> None:
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=1))
        Session.objects.filter(ticket=ticket).update(ended_at=timezone.now())
        assert pane.refreshed_state() == PaneState.IDLE

    def test_stopped_when_claim_cleared(self) -> None:
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
        pane.stop()
        assert pane.state == PaneState.STOPPED

        task.refresh_from_db()
        assert task.claimed_by == ""
        assert task.status != Task.Status.CLAIMED


class TestPaneTransitions(TestCase):
    def test_graceful_stop_teammate_idle_releases_claim(self) -> None:
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)

        pane.stop(reason="TeammateIdle")
        assert pane.state == PaneState.STOPPED
        task.refresh_from_db()
        assert task.claimed_by == ""

    def test_stop_is_idempotent(self) -> None:
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
        pane.stop()
        # A second stop on an already-stopped pane is a no-op, not an error.
        pane.stop()
        assert pane.state == PaneState.STOPPED

    def test_heartbeat_keeps_pane_active(self) -> None:
        ticket = _ticket()
        task = _pending_task(ticket)
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER, lease_seconds=300)
        pane.heartbeat(lease_seconds=600)
        task.refresh_from_db()
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > timezone.now() + timedelta(seconds=540)
        assert pane.refreshed_state() == PaneState.ACTIVE

    def test_spawn_runs_the_namespace_guard(self) -> None:
        ticket = _ticket()
        # Every role spawns into a ``team:<role>`` slot — the guard never raises.
        for role in TeamRole:
            task = _pending_task(ticket)
            pane = TeammatePane.spawn(task, role=role)
            assert pane.claim_slot == team_claim_slot(role)


def test_pane_state_enum_values_are_the_lifecycle() -> None:
    assert {s.value for s in PaneState} == {"spawn", "active", "idle", "stopped"}
