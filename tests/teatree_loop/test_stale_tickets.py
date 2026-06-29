"""DB-backed tests for ``StaleTicketsScanner`` (issue #563).

Staleness is derived purely from existing models — no new fields, no
external API calls. The scanner only *reports*; it never transitions a
ticket. ``TaskAttempt.started_at``/``TicketTransition.created_at`` are
``auto_now_add`` columns, so the tests backdate them with ``update()``
(real rows, no mocked teatree models).
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.loop.scanners.stale_tickets import StaleTicketsScanner


class StaleTicketsScannerTests(TestCase):
    OVERLAY = "acme"

    def _ticket(self, *, state: str = Ticket.State.STARTED, number: int = 42) -> Ticket:
        return Ticket.objects.create(
            overlay=self.OVERLAY,
            issue_url=f"https://example.com/issues/{number}",
            state=state,
        )

    def _backdate_transition(self, ticket: Ticket, *, days: int) -> None:
        tr = TicketTransition.objects.create(
            ticket=ticket,
            from_state=Ticket.State.NOT_STARTED,
            to_state=ticket.state,
        )
        TicketTransition.objects.filter(pk=tr.pk).update(
            created_at=timezone.now() - timedelta(days=days),
        )

    def _backdate_attempt(self, ticket: Ticket, *, days: int) -> None:
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session)
        attempt = TaskAttempt.objects.create(
            task=task,
            execution_target=Task.ExecutionTarget.HEADLESS,
            ended_at=timezone.now() - timedelta(days=days),
        )
        TaskAttempt.objects.filter(pk=attempt.pk).update(
            started_at=timezone.now() - timedelta(days=days),
        )

    def _scanner(self, *, threshold_days: int = 3) -> StaleTicketsScanner:
        return StaleTicketsScanner(overlay_name=self.OVERLAY, threshold_days=threshold_days)

    def test_no_signal_for_fresh_ticket(self) -> None:
        ticket = self._ticket()
        self._backdate_attempt(ticket, days=1)
        assert self._scanner().scan() == []

    def test_stale_by_last_attempt(self) -> None:
        ticket = self._ticket()
        self._backdate_attempt(ticket, days=5)
        signals = self._scanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "ticket.stale"
        assert signals[0].payload["ticket_id"] == ticket.pk
        assert signals[0].payload["age_days"] == 5
        assert signals[0].payload["ticket_state"] == Ticket.State.STARTED
        # Concise summary (no "stale in <state>" filler) — the statusline
        # collapses these into one linked line per overlay.
        assert signals[0].summary == f"#{ticket.ticket_number} stale (5d)"

    def test_stale_signal_carries_overlay_and_url_for_linking(self) -> None:
        """The statusline needs ``overlay`` to group and ``issue_url`` to link."""
        ticket = self._ticket()
        self._backdate_attempt(ticket, days=5)
        payload = self._scanner().scan()[0].payload
        assert payload["overlay"] == self.OVERLAY
        assert payload["issue_url"] == ticket.issue_url
        assert payload["stale"] is True

    def test_fresh_attempt_beats_old_transition(self) -> None:
        ticket = self._ticket()
        self._backdate_transition(ticket, days=10)
        self._backdate_attempt(ticket, days=1)
        assert self._scanner().scan() == []

    def test_falls_back_to_transition_when_no_attempts(self) -> None:
        ticket = self._ticket()
        self._backdate_transition(ticket, days=7)
        signals = self._scanner().scan()
        assert len(signals) == 1
        assert signals[0].payload["age_days"] == 7

    def test_no_activity_at_all_is_skipped(self) -> None:
        self._ticket()
        assert self._scanner().scan() == []

    def test_not_started_excluded(self) -> None:
        ticket = self._ticket(state=Ticket.State.NOT_STARTED)
        self._backdate_attempt(ticket, days=9)
        assert self._scanner().scan() == []

    def test_terminal_states_excluded(self) -> None:
        for state in (Ticket.State.MERGED, Ticket.State.DELIVERED, Ticket.State.IGNORED):
            ticket = self._ticket(state=state, number=100 + len(state))
            self._backdate_attempt(ticket, days=20)
        assert self._scanner().scan() == []

    def test_threshold_is_configurable(self) -> None:
        ticket = self._ticket()
        self._backdate_attempt(ticket, days=4)
        assert self._scanner(threshold_days=7).scan() == []
        signals = self._scanner(threshold_days=2).scan()
        assert len(signals) == 1
        assert signals[0].payload["age_days"] == 4

    def test_overlay_filter_isolates_other_overlays(self) -> None:
        mine = self._ticket(number=1)
        self._backdate_attempt(mine, days=8)
        other = Ticket.objects.create(
            overlay="other",
            issue_url="https://example.com/issues/2",
            state=Ticket.State.STARTED,
        )
        self._backdate_attempt(other, days=8)
        signals = self._scanner().scan()
        assert [s.payload["ticket_id"] for s in signals] == [mine.pk]

    def test_does_not_mutate_ticket_state(self) -> None:
        ticket = self._ticket()
        self._backdate_attempt(ticket, days=12)
        self._scanner().scan()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
