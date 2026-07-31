"""The dispatch-zone phase-task mint is idempotent in its side effects (#3969).

``create_phase_task`` is the mint for the phases ``Ticket._schedule_headless``
does not cover — ``debugging`` / ``e2e`` / ``answering`` / ``codex_reviewing`` and
the corrective ``coding`` re-entries. It used to mint unconditionally, so two
dispatchers that both passed a caller's ``has_open_task`` pre-check before either
wrote produced TWO Sessions and TWO Tasks for one ``(ticket, phase)`` — the same
defect #3903 fixed at the other mint.

The two directions are pinned separately, as at the sibling seam: an in-flight
sibling is a DUPLICATE and collapses; a terminal sibling is a finished attempt and
the next call is legitimate re-work that must still mint. A guard that also
swallowed the terminal case would let one crashed attempt wedge the phase forever.

Anti-vacuity: every collapse assertion below was observed RED against the pre-fix
mint (two rows where one is asserted), and every re-work assertion was observed RED
against a guard widened to dedupe on "a task ever existed".
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.loop.dispatch import DispatchAction
from teatree.loop.persistence import _handle_answerer
from teatree.loop.persistence_phase_task import create_phase_task, has_open_task, open_task_in_phase


class TestOpenTaskLookup(TestCase):
    """The pre-check the zone handlers short-circuit on: active-only, spelling-tolerant."""

    @staticmethod
    def _ticket() -> Ticket:
        return Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/1")

    def _store(self, ticket: Ticket, *, phase: str, status: str) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="coder")
        return Task.objects.create(ticket=ticket, session=session, phase=phase, status=status)

    def test_an_active_task_reads_as_open_in_any_spelling(self) -> None:
        ticket = self._ticket()
        stored = self._store(ticket, phase="code", status=Task.Status.CLAIMED)

        assert open_task_in_phase(ticket, phase="coding") == stored
        assert has_open_task(ticket, phase="coding")

    def test_a_terminal_task_does_not_read_as_open(self) -> None:
        ticket = self._ticket()
        self._store(ticket, phase="coding", status=Task.Status.COMPLETED)

        assert open_task_in_phase(ticket, phase="coding") is None
        assert has_open_task(ticket, phase="coding") is False


class TestPhaseTaskMintCollapsesDuplicates(TestCase):
    """A second mint for a ticket+phase that is already in flight returns the live task."""

    @staticmethod
    def _ticket(url: str = "answer://event/1") -> Ticket:
        return Ticket.objects.create(overlay="test", issue_url=url)

    def _mint(self, ticket: Ticket, *, phase: str = "debugging") -> Task:
        return create_phase_task(ticket, phase=phase, agent_id="debug", reason="Auto-scheduled red-MR fix")

    def test_two_mints_for_one_ticket_phase_yield_one_task(self) -> None:
        # The load-bearing case: two dispatchers that both read "no open task"
        # before either wrote. Pre-fix this minted a rival task.
        ticket = self._ticket()

        first = self._mint(ticket)
        second = self._mint(ticket)

        assert second.pk == first.pk, "the second mint created a rival task for the same ticket+phase"
        assert Task.objects.filter(ticket=ticket, phase="debugging").count() == 1

    def test_a_deduped_mint_leaves_no_orphan_session(self) -> None:
        # The Session is minted first, so a guard placed AFTER it would collapse the
        # Task and still leak a session row per raced dispatcher.
        ticket = self._ticket()

        self._mint(ticket)
        self._mint(ticket)

        assert Session.objects.filter(ticket=ticket).count() == 1

    def test_a_claimed_sibling_is_a_duplicate_not_a_retry(self) -> None:
        # A live lease is the strongest possible "already being worked".
        ticket = self._ticket()
        first = self._mint(ticket)
        first.status = Task.Status.CLAIMED
        first.save(update_fields=["status"])

        second = self._mint(ticket)

        assert second.pk == first.pk
        assert Task.objects.filter(ticket=ticket, phase="debugging").count() == 1

    def test_a_short_verb_row_dedupes_against_the_gerund_phase(self) -> None:
        # ``tasks create <id> code`` stores the short verb; the corrective coding
        # re-entries mint the gerund. One phase, so one lock — or each spelling
        # mints a rival for the other.
        ticket = self._ticket("skill-drift://o/r/f.md")
        session = Session.objects.create(ticket=ticket, agent_id="skill-drift")
        stored = Task.objects.create(ticket=ticket, session=session, phase="code")

        minted = create_phase_task(ticket, phase="coding", agent_id="skill-drift", reason="Auto-scheduled fix")

        assert minted.pk == stored.pk
        assert Task.objects.filter(ticket=ticket).count() == 1

    def test_every_zone_phase_collapses(self) -> None:
        ticket = self._ticket()

        for phase in ("debugging", "e2e", "answering", "codex_reviewing"):
            assert self._mint(ticket, phase=phase).pk == self._mint(ticket, phase=phase).pk

        assert Task.objects.filter(ticket=ticket).count() == 4

    def test_raced_dispatchers_past_the_caller_precheck_mint_once(self) -> None:
        # End to end through a real zone handler, with the caller's read-then-write
        # pre-check forced to the value BOTH dispatchers read in the race: no open
        # task. The pre-check is no longer load-bearing for correctness.
        action = DispatchAction(
            kind="agent",
            zone="t3:answerer",
            detail="inbound question",
            payload={"event_id": 77, "overlay": "test"},
        )
        with patch("teatree.loop.persistence.has_open_task", return_value=False):
            first = _handle_answerer(action)
            second = _handle_answerer(action)

        assert first is not None
        assert second is not None
        assert second.pk == first.pk
        assert Task.objects.filter(phase="answering").count() == 1
        assert Session.objects.count() == 1


class TestPhaseTaskMintStillMintsLegitimateRework(TestCase):
    """The guard is not over-eager: a finished attempt never blocks the next one."""

    @staticmethod
    def _ticket(url: str = "e2e-failure://test/spec.ts") -> Ticket:
        return Ticket.objects.create(overlay="test", issue_url=url)

    def _mint(self, ticket: Ticket, *, phase: str = "e2e") -> Task:
        return create_phase_task(ticket, phase=phase, agent_id="e2e-fix", reason="Auto-scheduled E2E fix")

    def test_a_failed_attempt_is_followed_by_a_fresh_task(self) -> None:
        # The wedge-forever direction: a crashed worker's FAILED task must not
        # suppress every later attempt at the same phase.
        ticket = self._ticket()
        first = self._mint(ticket)
        first.status = Task.Status.FAILED
        first.save(update_fields=["status"])

        second = self._mint(ticket)

        assert second.pk != first.pk, "a failed attempt blocked its own retry"
        assert second.session_id != first.session_id, "re-work must run in a fresh session"

    def test_a_completed_attempt_is_followed_by_a_fresh_task(self) -> None:
        ticket = self._ticket()
        first = self._mint(ticket)
        first.status = Task.Status.COMPLETED
        first.save(update_fields=["status"])

        assert self._mint(ticket).pk != first.pk

    def test_every_terminal_status_is_followed_by_a_fresh_task(self) -> None:
        # ``Status.terminal()`` is the SSOT for "finished"; every member of it is
        # re-work, so the guard is keyed on the active half of that partition.
        for index, status in enumerate(sorted(Task.Status.terminal())):
            ticket = self._ticket(f"e2e-failure://test/terminal-{index}.ts")
            first = self._mint(ticket)
            first.status = status
            first.save(update_fields=["status"])

            assert self._mint(ticket).pk != first.pk, f"a {status} attempt blocked its own retry"

    def test_distinct_phases_never_dedupe_against_each_other(self) -> None:
        ticket = self._ticket()

        debugging = self._mint(ticket, phase="debugging")
        e2e = self._mint(ticket, phase="e2e")

        assert debugging.pk != e2e.pk
        assert Task.objects.filter(ticket=ticket).count() == 2

    def test_a_sibling_ticket_in_the_same_phase_is_not_a_duplicate(self) -> None:
        # Overlay scoping is NOT asserted here: this seam narrows by ``ticket``,
        # which already implies the overlay. That axis is pinned at the manager
        # (``test_in_flight_for_phase_scopes_to_overlay_and_phase``).
        mine = self._ticket()
        theirs = self._ticket("e2e-failure://test/other.ts")

        assert self._mint(mine).pk != self._mint(theirs).pk
        assert Task.objects.filter(phase="e2e").count() == 2
