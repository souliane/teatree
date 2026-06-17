"""Per-pane heartbeat / DB recovery for the inert pane layer (#1838 PR#7a).

A pane renews its lease via the existing ``Task.renew_lease`` heartbeat, so the
standing claim-lease recovery primitives (``reclaim_orphaned_claims`` /
``reap_stale_claims``) still reach a dead pane's claim — the DB stays the single
source of truth. A live (heartbeated) pane is never recovered out from under a
running teammate; a pane that stops heartbeating is.
"""

import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.teams.panes import TeammatePane
from teatree.teams.roles import TeamRole, team_claim_slot


def _pane_task() -> Task:
    ticket = Ticket.objects.create(overlay="", issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")
    session = Session.objects.create(ticket=ticket, agent_id="a")
    return Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)


class TestPaneHeartbeatRecovery(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # ``TeammatePane.spawn`` fails closed when teams is off; these recovery
        # tests exercise the live-pane lifecycle, which only runs when teams is on.
        ConfigSetting.objects.set_value("teams_enabled", value=True)

    def test_live_heartbeated_pane_is_not_recovered(self) -> None:
        task = _pane_task()
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER, lease_seconds=300)
        pane.heartbeat(lease_seconds=300)

        # The standing sweeps run; a live lease is left with its pane.
        assert Task.objects.reclaim_orphaned_claims() == 0
        assert Task.objects.reap_stale_claims() == 0
        task.refresh_from_db()
        assert task.claimed_by == team_claim_slot(TeamRole.CORE_MAKER)
        assert task.status == Task.Status.CLAIMED

    def test_dead_pane_claim_is_reclaimed_to_pending(self) -> None:
        task = _pane_task()
        TeammatePane.spawn(task, role=TeamRole.CORE_MAKER, lease_seconds=300)
        # Pane stopped heartbeating — its lease has lapsed.
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=1))

        # reclaim_orphaned_claims returns the dead pane's claim to PENDING so the
        # loop can re-surface it — the DB recovers it without the pane's help.
        recovered = Task.objects.reclaim_orphaned_claims()
        assert recovered == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.claimed_by == ""

    def test_dead_pane_claim_is_reaped_when_not_reclaimed_first(self) -> None:
        task = _pane_task()
        TeammatePane.spawn(task, role=TeamRole.CORE_MAKER, lease_seconds=300)
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timedelta(minutes=1))

        # reap_stale_claims fails the still-expired claim — the dead pane's
        # claim is never stuck CLAIMED forever.
        reaped = Task.objects.reap_stale_claims()
        assert reaped == 1
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.claimed_by == ""
