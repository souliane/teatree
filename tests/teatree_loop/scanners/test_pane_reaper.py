"""Pane-reaper scanner — demote idle maker panes when teams is enabled (#1838 PR#7b).

The consumer-side wiring PR#7a deferred: a global scanner that, ONLY when
``teams_enabled``, demotes idle maker panes (a ``team:<role>`` claim with no live
Session past ``teams_idle_minutes``) to STOPPED via
:func:`teatree.teams.pane_reaper.reap_idle_panes`, emitting one
``team_pane.reaped`` signal per demotion. DEFAULT-OFF: when teams is disabled the
scanner is a no-op (it never even touches the pane reaper).
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket
from teatree.loop.scanners.pane_reaper import PaneReaperScanner
from teatree.teams.panes import PaneState, TeammatePane
from teatree.teams.roles import TeamRole, team_claim_slot


def _idle_pane_task() -> Task:
    ticket = Ticket.objects.create(overlay="", issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")
    session = Session.objects.create(ticket=ticket, agent_id="a")
    task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)
    TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
    stamp = timezone.now() - timedelta(minutes=45)
    Task.objects.filter(pk=task.pk).update(heartbeat_at=stamp, lease_expires_at=stamp)
    Session.objects.filter(ticket=ticket).update(ended_at=timezone.now())
    task.refresh_from_db()
    return task


class TestPaneReaperScannerEnabled(TestCase):
    def test_demotes_an_idle_pane_and_emits_a_signal(self) -> None:
        task = _idle_pane_task()
        scanner = PaneReaperScanner(teams_enabled=True, idle_minutes=30)
        signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].kind == "team_pane.reaped"
        task.refresh_from_db()
        assert task.claimed_by == ""
        pane = TeammatePane(task, role=TeamRole.CORE_MAKER)
        assert pane.refreshed_state() == PaneState.STOPPED

    def test_keeps_a_fresh_pane(self) -> None:
        ticket = Ticket.objects.create(overlay="", issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)
        TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
        Session.objects.filter(ticket=ticket).update(ended_at=timezone.now())

        scanner = PaneReaperScanner(teams_enabled=True, idle_minutes=30)
        assert scanner.scan() == []
        task.refresh_from_db()
        assert task.claimed_by == team_claim_slot(TeamRole.CORE_MAKER)


class TestPaneReaperScannerDisabled(TestCase):
    def test_disabled_scanner_is_a_no_op(self) -> None:
        task = _idle_pane_task()
        scanner = PaneReaperScanner(teams_enabled=False, idle_minutes=30)
        assert scanner.scan() == []
        # The idle pane is UNTOUCHED — the reaper is never invoked when off.
        task.refresh_from_db()
        assert task.claimed_by == team_claim_slot(TeamRole.CORE_MAKER)

    def test_disabled_scanner_never_calls_reap_idle_panes(self) -> None:
        _idle_pane_task()
        scanner = PaneReaperScanner(teams_enabled=False, idle_minutes=30)
        with patch("teatree.loop.scanners.pane_reaper.reap_idle_panes") as reaper:
            scanner.scan()
        reaper.assert_not_called()
