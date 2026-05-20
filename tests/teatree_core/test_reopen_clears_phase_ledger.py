"""#1286 — ``Ticket.reopen()`` must clear the prior workstream's phase ledger.

Codex finding (umbrella #1282 item 3, high blast): the shipping gate
consumes ``Ticket.aggregate_phase_records()`` — the UNION of
``visited_phases`` across every session attached to the ticket. When a
ticket is reused for a second workstream (the FSM ``reopen()`` from
SHIPPED/MERGED/IN_REVIEW/RETROSPECTED, or the idempotent
``workspace ticket <url>`` on a previously-shipped ticket), the prior
workstream's ``testing``/``reviewing`` attestations remain in the union
and false-pass the new workstream's gate. ``AGENTS.md`` § "Reused-ticket
attestation" documents this as a known risk but the FSM did not actually
guard it.

The fix: ``reopen()`` retires every session's phase ledger for the
ticket (the same operation the sanctioned ``lifecycle clear-ledger
--confirm`` performs) so the next workstream re-earns its attestations
from scratch. ``reopen()`` is the explicit "previous workstream done,
start fresh" signal in the FSM — the natural workstream-boundary hook.
"""

import pytest
from django.test import TestCase

from teatree.core.models import Session, Ticket
from teatree.core.models.errors import QualityGateError


def _ticket(**kw: object) -> Ticket:
    return Ticket.objects.create(overlay="test", **kw)


class TestReopenClearsPhaseLedger(TestCase):
    """``Ticket.reopen()`` retires every session's phase ledger.

    The previous workstream's attestations cannot satisfy the next
    workstream's shipping gate.
    """

    def test_reopen_clears_prior_workstream_visited_phases(self) -> None:
        ticket = _ticket(state=Ticket.State.SHIPPED)
        prior = Session.objects.create(ticket=ticket, agent_id="prior-loop")
        prior.visit_phase("coding", agent_id="prior-loop")
        prior.visit_phase("testing", agent_id="prior-loop")
        prior.visit_phase("reviewing", agent_id="prior-reviewer")

        ticket.reopen()
        ticket.save()

        prior.refresh_from_db()
        assert prior.visited_phases == [], (
            f"reopen() must retire the prior workstream's visited_phases; got {prior.visited_phases!r}"
        )
        assert prior.phase_visits == {}, (
            f"reopen() must retire the prior workstream's phase_visits audit trail; got {prior.phase_visits!r}"
        )

    def test_reopen_clears_repos_modified_and_tested(self) -> None:
        ticket = _ticket(state=Ticket.State.MERGED)
        prior = Session.objects.create(ticket=ticket, agent_id="prior-loop")
        prior.mark_repo_modified("backend")
        prior.mark_repo_tested("backend")

        ticket.reopen()
        ticket.save()

        prior.refresh_from_db()
        assert prior.repos_modified == [], (
            f"reopen() must retire the prior workstream's repos_modified; got {prior.repos_modified!r}"
        )
        assert prior.repos_tested == [], (
            f"reopen() must retire the prior workstream's repos_tested; got {prior.repos_tested!r}"
        )

    def test_reopen_clears_every_session_across_the_ticket(self) -> None:
        ticket = _ticket(state=Ticket.State.SHIPPED)
        s1 = Session.objects.create(ticket=ticket, agent_id="coding")
        s1.visit_phase("coding", agent_id="coding")
        s2 = Session.objects.create(ticket=ticket, agent_id="testing")
        s2.visit_phase("testing", agent_id="testing")
        s3 = Session.objects.create(ticket=ticket, agent_id="reviewing")
        s3.visit_phase("reviewing", agent_id="reviewer")

        ticket.reopen()
        ticket.save()

        for session in (s1, s2, s3):
            session.refresh_from_db()
            assert session.visited_phases == [], (
                f"session {session.pk} retained visited_phases after reopen(): {session.visited_phases!r}"
            )

    def test_reopen_makes_next_workstream_re_earn_gate(self) -> None:
        """The integration assertion the codex finding pins.

        Prior workstream is fully attested; ``reopen()`` then a fresh
        coding-only session for the next workstream must **not** false-pass
        the shipping gate on the prior workstream's testing/reviewing.
        """
        ticket = _ticket(state=Ticket.State.SHIPPED)
        prior = Session.objects.create(ticket=ticket, agent_id="prior-loop")
        prior.visit_phase("coding", agent_id="prior-loop")
        prior.visit_phase("testing", agent_id="prior-loop")
        prior.visit_phase("reviewing", agent_id="prior-reviewer")

        # The aggregate ledger pre-reopen falsely satisfies the gate
        # for any future session (no fresh attestation needed).
        prior.check_gate_across_ticket("shipping")

        ticket.reopen()
        ticket.save()

        # New workstream: only ``coding`` recorded so far.
        new_session = Session.objects.create(ticket=ticket, agent_id="new-loop")
        new_session.visit_phase("coding", agent_id="new-loop")

        with pytest.raises(QualityGateError, match="testing"):
            new_session.check_gate_across_ticket("shipping")

        # Earning the new workstream's testing + reviewing makes the
        # gate pass — proving the fix is satisfiable, not a hard block.
        new_session.visit_phase("testing", agent_id="new-loop")
        new_session.visit_phase("reviewing", agent_id="new-reviewer")
        new_session.check_gate_across_ticket("shipping")
