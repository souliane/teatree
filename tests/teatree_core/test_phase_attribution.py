"""#833 — the gate enforces phase-presence only; no agent_id inference.

Independence in code review is a property of the EXECUTION CONTEXT:
review runs in a freshly-spawned cold-review sub-agent that has not
seen the implementation. That spawn boundary is the independence
guarantee, by construction — same-session spawn is fine. The old
``agent_id``-string maker≠checker inference (same-agent ⇒ violation;
blank-agent_id ⇒ fail-closed) added no real independence and was
net-negative (it false-denied legitimate same-session work). It was
removed; only the phase-presence gate remains.

``phase_visits`` is kept purely as an audit trail of who recorded each
phase. It is not consumed for gate enforcement.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Session, Ticket
from teatree.core.models.errors import QualityGateError


def _ticket(**kw: object) -> Ticket:
    return Ticket.objects.create(overlay="test", **kw)


class TestNoAgentIdInference(TestCase):
    def test_same_agent_coding_and_reviewing_does_not_raise(self) -> None:
        # The realistic same-session-sub-agent case: coding and reviewing
        # recorded under the same agent_id. Pre-#833 this raised
        # "Maker≠checker violation"; now phases-present ⇒ pass.
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="loop")
        session.visit_phase("coding", agent_id="loop")
        session.visit_phase("testing", agent_id="loop")
        session.visit_phase("reviewing", agent_id="loop")
        session.visit_phase("retro", agent_id="loop")

        session.check_gate_across_ticket("shipping")

    def test_blank_agent_id_unattributed_phases_do_not_raise(self) -> None:
        # Pre-#833 the blank-agent_id path failed CLOSED ("unverifiable").
        # Now an empty phase_visits audit trail does not block the gate.
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket)  # agent_id=""
        session.visited_phases = ["testing", "coding", "reviewing", "retro"]
        session.phase_visits = {}
        session.save(update_fields=["visited_phases", "phase_visits"])

        session.check_gate_across_ticket("shipping")

    def test_same_agent_reviewing_gate_on_own_session_does_not_raise(self) -> None:
        ticket = _ticket(state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, agent_id="same")
        session.visit_phase("testing", agent_id="same")
        session.visit_phase("coding", agent_id="same")
        session.visit_phase("reviewing", agent_id="same")

        session.check_gate("reviewing")


class TestPhasePresenceStillEnforced(TestCase):
    def test_missing_reviewing_phase_still_raises(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="loop")
        session.visit_phase("testing", agent_id="loop")
        session.visit_phase("retro", agent_id="loop")

        with pytest.raises(QualityGateError, match="reviewing"):
            session.check_gate_across_ticket("shipping")

    def test_missing_testing_for_reviewing_still_raises(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="loop")

        with pytest.raises(QualityGateError, match="testing"):
            session.check_gate("reviewing")

    def test_present_phases_scattered_across_sessions_pass(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        s1 = Session.objects.create(ticket=ticket, agent_id="a")
        s1.visit_phase("testing", agent_id="a")
        s2 = Session.objects.create(ticket=ticket, agent_id="a")
        s2.visit_phase("reviewing", agent_id="a")
        s3 = Session.objects.create(ticket=ticket, agent_id="a")
        s3.visit_phase("retro", agent_id="a")

        s3.check_gate_across_ticket("shipping")

    def test_force_bypasses_phase_presence(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket)

        session.check_gate("shipping", force=True)


class TestPhaseVisitsRemainsAuditTrail(TestCase):
    def test_cli_visit_phase_records_attribution(self) -> None:
        # Generic session-derived attribution (a non-`reviewing` phase — the
        # `reviewing` visit requires an explicit reviewer id per §17.6
        # candidate 13, covered separately).
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="cli-actor")

        call_command("lifecycle", "visit-phase", str(ticket.pk), "brainstorm")

        session.refresh_from_db()
        assert "brainstorm" in session.visited_phases
        assert session.phase_visits["brainstorm"]["agent_id"] == "cli-actor"

    def test_explicit_agent_id_recorded_verbatim(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket)

        call_command("lifecycle", "visit-phase", str(ticket.pk), "review", "--agent-id", "alice@cli")

        session = ticket.sessions.order_by("-pk").first()
        assert session is not None
        assert session.phase_visits["reviewing"]["agent_id"] == "alice@cli"

    def test_blank_session_still_attributes_non_empty(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket)  # agent_id=""

        call_command("lifecycle", "visit-phase", str(ticket.pk), "brainstorm")

        session = ticket.sessions.order_by("-pk").first()
        assert session is not None
        assert session.phase_visits["brainstorm"]["agent_id"]


class TestVisitPhaseConcurrentWriteDoesNotLoseUpdate(TestCase):
    """``visit_phase``'s read-modify-write must stay atomic + row-locked.

    Two concurrent writers on the same Session row must not lose-update
    (the maker ``loop`` session live while an independent reviewer
    records ``reviewing`` on the same pk — observed clobber).
    Deterministic interleave: a competing writer commits its phase at the
    lock-acquire point. With the lock the second writer re-reads the
    locked row and MERGES; both phases survive.
    """

    def test_concurrent_writers_both_phases_survive(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="maker")
        session.visit_phase("coding", agent_id="maker")

        reviewer_view = Session.objects.get(pk=session.pk)

        rival_done: list[str] = []
        original_sfu = type(session).objects.select_for_update

        def rival_then_lock(*args: object, **kwargs: object) -> object:
            if not rival_done:
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
        assert "testing" in fresh.visited_phases, "competitor's write was lost (no row lock — clobber)"
        assert "reviewing" in fresh.visited_phases, "reviewer's write was lost"
        assert fresh.phase_visits.get("testing", {}).get("agent_id") == "maker"
        assert fresh.phase_visits.get("reviewing", {}).get("agent_id") == "reviewer"
