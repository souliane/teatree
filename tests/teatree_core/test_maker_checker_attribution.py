"""#755 — maker≠checker must be mechanically verifiable and fail-CLOSED.

The pre-existing tests cover the case where ``Session.agent_id`` is
*non-empty*. The #755 gap is the opposite: when the recording session's
``agent_id`` is blank (``Session.objects.create(ticket=...)`` fallback,
coordinator/non-FSM-minted sessions), ``visit_phase`` skips the
``phase_visits`` stamp entirely, so ``_check_maker_checker`` sees no
attribution and **silently passes** — the safety check cannot observe
what it must enforce (fail-OPEN). These tests pin the inversion:

Pinned: absent per-phase attribution at gate time must FAIL-CLOSED
(raise), not vacuously pass; the CLI path stamps a non-empty identity
even with no explicit ``--agent-id`` and no ``Session.agent_id`` (never
``""``); an explicit ``--agent-id`` is recorded verbatim; a genuinely
distinct maker/checker still passes (no over-block); the loop path is
symmetric (a blank-agent_id session still yields non-empty attribution).

RED-first: each assertion is verified to fail on current ``main`` before
the fix.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Session, Ticket
from teatree.core.models.errors import QualityGateError


def _ticket(**kw: object) -> Ticket:
    return Ticket.objects.create(overlay="test", **kw)


class TestGateFailsClosedOnAbsentAttribution(TestCase):
    def test_conflicting_phases_present_but_unattributed_raises(self) -> None:
        # Both conflicting phases are in visited_phases but phase_visits
        # is empty (the blank-agent_id path). Pre-fix: _check_maker_checker
        # `continue`s past the missing pair and the gate vacuously passes.
        # Post-fix: it must fail-CLOSED — work claims done with zero
        # maker attribution is unverifiable, so refuse.
        ticket = _ticket(state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket)  # agent_id="" (model default)
        # Force the unattributed state directly: visited but no phase_visits.
        session.visited_phases = ["testing", "coding", "reviewing"]
        session.phase_visits = {}
        session.save(update_fields=["visited_phases", "phase_visits"])

        with pytest.raises(QualityGateError, match=r"(?i)unverifiable|attribution"):
            session.check_gate("reviewing")

    def test_attributed_distinct_agents_still_passes(self) -> None:
        # Regression guard: a genuine maker≠checker must NOT be over-blocked
        # by the new fail-closed rule.
        ticket = _ticket(state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, agent_id="maker")
        session.visited_phases = ["testing", "coding", "reviewing"]
        session.phase_visits = {
            "coding": {"agent_id": "maker", "timestamp": "t"},
            "reviewing": {"agent_id": "checker", "timestamp": "t"},
        }
        session.save(update_fields=["visited_phases", "phase_visits"])

        session.check_gate("reviewing")  # must not raise

    def test_attributed_same_agent_still_trips_violation(self) -> None:
        ticket = _ticket(state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, agent_id="same")
        session.visited_phases = ["testing", "coding", "reviewing"]
        session.phase_visits = {
            "coding": {"agent_id": "same", "timestamp": "t"},
            "reviewing": {"agent_id": "same", "timestamp": "t"},
        }
        session.save(update_fields=["visited_phases", "phase_visits"])

        with pytest.raises(QualityGateError, match="Maker≠checker violation"):
            session.check_gate("reviewing")


class TestCliStampsNonEmptyIdentity(TestCase):
    def test_visit_phase_without_agent_id_and_blank_session_still_attributes(self) -> None:
        # The #755 core: a session with blank agent_id, recorded via the
        # CLI with no --agent-id, must STILL land a non-empty identity in
        # phase_visits (never "" → never silently skipped).
        ticket = _ticket(state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket)  # agent_id=""

        call_command("lifecycle", "visit-phase", str(ticket.pk), "review")

        session = ticket.sessions.order_by("-pk").first()
        assert session is not None
        assert "reviewing" in session.phase_visits, (
            "blank-agent_id CLI visit did not stamp phase_visits — maker≠checker is unverifiable on this path (#755)"
        )
        assert session.phase_visits["reviewing"]["agent_id"], "attribution must be non-empty"

    def test_explicit_agent_id_is_recorded_verbatim(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket)

        call_command("lifecycle", "visit-phase", str(ticket.pk), "review", "--agent-id", "alice@cli")

        session = ticket.sessions.order_by("-pk").first()
        assert session is not None
        assert session.phase_visits["reviewing"]["agent_id"] == "alice@cli"


class TestLoopPathSymmetricAttribution(TestCase):
    def test_loop_record_phase_visit_blank_session_still_attributes(self) -> None:
        # The loop path (Task._record_phase_visit) has the same
        # session.agent_id dependency; a blank-agent_id session must not
        # produce a blank attribution there either (#755 point 3).
        from teatree.core.models import Task  # noqa: PLC0415

        ticket = _ticket(state=Ticket.State.TESTED)
        session = Session.objects.create(ticket=ticket)  # agent_id=""
        task = Task.objects.create(ticket=ticket, session=session, phase="review")

        task._record_phase_visit()

        session.refresh_from_db()
        assert "reviewing" in session.phase_visits, (
            "loop path left blank attribution — asymmetric with the CLI fix (#755)"
        )
        assert session.phase_visits["reviewing"]["agent_id"], "attribution must be non-empty"


class TestVisitPhaseConcurrentWriteDoesNotLoseUpdate(TestCase):
    """#755: ``visit_phase``'s read-modify-write must be atomic + row-locked.

    Two concurrent writers on the same Session row must not lose-update
    (the maker ``loop`` session live while an independent reviewer
    records ``reviewing`` on the same pk — observed clobber).
    Deterministic interleave (mirrors the #748 ``ensure_session`` test):
    a competing writer commits its phase at the lock-acquire point. With
    the lock the second writer re-reads the locked row and MERGES; both
    phases survive. Pre-fix (unlocked, stale in-memory read-modify-write)
    the second ``.update`` overwrites the competitor's column → its
    phase is lost.
    """

    def test_concurrent_writers_both_phases_survive(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="maker")
        # Seed the "maker already recorded coding" state.
        session.visit_phase("coding", agent_id="maker")

        # A second in-memory handle on the SAME row (the reviewer's view),
        # loaded before the rival write — the stale-read the race needs.
        reviewer_view = Session.objects.get(pk=session.pk)

        rival_done: list[str] = []
        original_sfu = type(session).objects.select_for_update

        def rival_then_lock(*args: object, **kwargs: object):
            if not rival_done:
                # Competitor commits a NEW phase on the same row while the
                # reviewer is mid-visit_phase (separate write, post the
                # reviewer's stale read).
                Session.objects.filter(pk=session.pk).update(
                    visited_phases=["coding", "testing"],
                    phase_visits={
                        "coding": {"agent_id": "maker", "timestamp": "t"},
                        "testing": {"agent_id": "maker", "timestamp": "t"},
                    },
                )
                rival_done.append("x")
            return original_sfu(*args, **kwargs)

        with patch.object(type(session).objects, "select_for_update", rival_then_lock):
            reviewer_view.visit_phase("reviewing", agent_id="reviewer")

        fresh = Session.objects.get(pk=session.pk)
        # Lost-update guard: the competitor's `testing` AND the reviewer's
        # `reviewing` must both survive (lock => re-read + merge, not clobber).
        assert "testing" in fresh.visited_phases, "competitor's write was lost (no row lock — clobber)"
        assert "reviewing" in fresh.visited_phases, "reviewer's write was lost"
        assert fresh.phase_visits.get("testing", {}).get("agent_id") == "maker"
        assert fresh.phase_visits.get("reviewing", {}).get("agent_id") == "reviewer"
