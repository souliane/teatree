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

from teatree.config import TeamsDisplay, UserSettings
from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.loop.global_scanner_factories import _pane_reaper_scanner
from teatree.loop.scanners.pane_reaper import PaneReaperScanner
from teatree.teams.panes import PaneState, TeammatePane
from teatree.teams.roles import TeamRole, team_claim_slot


def _idle_pane_task() -> Task:
    # ``TeammatePane.spawn`` fails closed when teams is off; a live pane (the
    # reaper scanner's subject) only exists once the master switch is on. The
    # scanner's own ``teams_enabled`` constructor arg is independent of this.
    ConfigSetting.objects.set_value("teams_enabled", value=True)
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
        ConfigSetting.objects.set_value("teams_enabled", value=True)
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


class TestPaneReaperScannerDisplayTeardown(TestCase):
    """The DB-driven tmux teardown rides the demote-to-STOPPED transition (WI-5)."""

    def test_demotion_reconciles_tmux_panes_against_live_claims(self) -> None:
        self._second_live_pane()  # a still-live team claim that must NOT be reaped.
        self._idle_pane()  # the idle pane demoted this tick.
        scanner = PaneReaperScanner(teams_enabled=True, idle_minutes=30, display_enabled=True)
        with patch("teatree.loop.scanners.pane_reaper.reconcile_orphan_panes") as reconcile:
            reconcile.return_value = []
            scanner.scan()
        # After the demotion, the surviving live team claim is the only authoritative
        # slot — the reconcile kills any tmux pane NOT in that set (the demoted one).
        reconcile.assert_called_once()
        live = reconcile.call_args.kwargs["live_claim_slots"]
        assert live == {team_claim_slot(TeamRole.OVERLAY_MAKER)}

    def test_no_tmux_teardown_when_display_disabled(self) -> None:
        # display_enabled defaults off → byte-identical to the pre-WI-5 reaper:
        # the DB demotion still happens, but tmux is never touched.
        self._idle_pane()
        scanner = PaneReaperScanner(teams_enabled=True, idle_minutes=30)
        with patch("teatree.loop.scanners.pane_reaper.reconcile_orphan_panes") as reconcile:
            signals = scanner.scan()
        reconcile.assert_not_called()
        assert len(signals) == 1

    def test_no_tmux_teardown_when_nothing_reaped(self) -> None:
        # A tick that reaps nothing must not poke tmux even with display on.
        scanner = PaneReaperScanner(teams_enabled=True, idle_minutes=30, display_enabled=True)
        with patch("teatree.loop.scanners.pane_reaper.reconcile_orphan_panes") as reconcile:
            assert scanner.scan() == []
        reconcile.assert_not_called()

    def test_reconcile_failure_does_not_break_the_tick(self) -> None:
        # A tmux reconcile blow-up is best-effort: the DB demotion + signal stand,
        # the exception never propagates into the loop tick.
        self._idle_pane()
        scanner = PaneReaperScanner(teams_enabled=True, idle_minutes=30, display_enabled=True)
        with patch(
            "teatree.loop.scanners.pane_reaper.reconcile_orphan_panes",
            side_effect=RuntimeError("tmux exploded"),
        ):
            signals = scanner.scan()
        assert len(signals) == 1

    def _idle_pane(self) -> Task:
        return _idle_pane_task()

    def _second_live_pane(self) -> Task:
        ConfigSetting.objects.set_value("teams_enabled", value=True)
        ticket = Ticket.objects.create(overlay="x", issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")
        session = Session.objects.create(ticket=ticket, agent_id="b")
        task = Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)
        TeammatePane.spawn(task, role=TeamRole.OVERLAY_MAKER)
        return task


class TestPaneReaperFactoryDisplayResolution(TestCase):
    """The factory threads ``teams_display`` into the scanner's ``display_enabled``."""

    def test_display_enabled_when_display_is_tmux(self) -> None:
        settings = UserSettings(teams_enabled=True, teams_display=TeamsDisplay.TMUX)
        with patch(
            "teatree.loop.global_scanner_factories.get_effective_settings",
            return_value=settings,
        ):
            scanner = _pane_reaper_scanner()
        assert scanner is not None
        assert scanner.display_enabled is True

    def test_display_disabled_when_display_is_none(self) -> None:
        settings = UserSettings(teams_enabled=True, teams_display=TeamsDisplay.NONE)
        with patch(
            "teatree.loop.global_scanner_factories.get_effective_settings",
            return_value=settings,
        ):
            scanner = _pane_reaper_scanner()
        assert scanner is not None
        assert scanner.display_enabled is False

    def test_factory_returns_none_when_teams_off(self) -> None:
        settings = UserSettings(teams_enabled=False, teams_display=TeamsDisplay.TMUX)
        with patch(
            "teatree.loop.global_scanner_factories.get_effective_settings",
            return_value=settings,
        ):
            assert _pane_reaper_scanner() is None
