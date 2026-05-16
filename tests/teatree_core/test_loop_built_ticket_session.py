"""#748 — loop/coordinator-built features must be representable in the FSM.

Two coupled defects, both reproduced here as failing tests first (TDD).

Defect A — the loop/coordinator path creates a Ticket (``get_or_create``
in the dispatch / ``tasks create`` path) with no Session row, so
``Ticket.aggregate_phase_records()`` is empty and the shipping gate
fail-CLOSES ("no session: no attested work").

Defect B — ``workspace ticket`` rolls back a failed provision with
``ticket.delete()``; ``Session.ticket`` is ``on_delete=CASCADE``, so a
concurrent ``lifecycle visit-phase`` that lazily created phase-attestation
sessions on that same (``get_or_create``-shared) Ticket has those sessions
cascade-reaped (observed: sessions created then gone, Ticket left with
zero sessions).
"""

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from teatree.core.models import Session, Ticket
from tests.teatree_core.test_new_management_commands import FULL_OVERLAY, SETTINGS, _patch_overlays


class TestProvisionRollbackPreservesAttestationSessions(TestCase):
    """A failed-provision rollback must not cascade-reap attestation.

    The Sessions holding genuine phase attestation for the ticket must
    survive the ``ticket.delete()`` rollback path (Defect B).
    """

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failed_provision_rollback_keeps_phase_sessions(self) -> None:
        # A loop-built ticket already carrying attested work: a session
        # with recorded phases (the maker≠checker ledger the gate reads).
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/748")
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        session.visit_phase("testing", agent_id="coding")
        session.visit_phase("retro", agent_id="coding")
        attested_session_pk = session.pk
        assert ticket.aggregate_phase_records()[0] == ["testing", "retro"]

        # A concurrent `workspace ticket` for the SAME issue_url resolves
        # the SAME ticket via get_or_create, then provisioning fails with
        # no worktrees → the rollback path (`ticket.delete()`) fires.
        with patch(
            "teatree.core.management.commands.workspace.WorktreeProvisioner",
        ) as prov:
            prov.return_value.run.return_value = type("R", (), {"ok": False, "detail": "provision boom"})()
            call_command("workspace", "ticket", "https://example.com/issues/748")

        # The ticket-with-attestation must still exist (or the attested
        # session must survive on whatever ticket represents this issue).
        surviving = Session.objects.filter(pk=attested_session_pk).first()
        assert surviving is not None, (
            "Defect B: the phase-attestation Session was cascade-reaped by "
            "the failed-provision rollback (`ticket.delete()` + CASCADE)."
        )
        # And the phase ledger the gate reads must be intact.
        live_ticket = Ticket.objects.filter(issue_url="https://example.com/issues/748").first()
        assert live_ticket is not None
        assert live_ticket.aggregate_phase_records()[0] == ["testing", "retro"]


class TestLoopBuiltTicketGetsDurableSession(TestCase):
    """A ticket created outside `workspace ticket` still gets a session.

    The loop / coordinator / tasks-create path must materialise a
    durable Session so phase records have a home and the gate can run
    instead of fail-closing (Defect A).
    """

    def test_loop_dispatch_ticket_has_a_session(self) -> None:
        # Mirror the loop dispatch path: Ticket via get_or_create, no
        # explicit Session creation by the caller.
        ticket, _ = Ticket.objects.get_or_create(
            issue_url="https://github.com/souliane/teatree/issues/999",
            defaults={"role": Ticket.Role.AUTHOR},
        )
        ticket.ensure_session()

        assert ticket.sessions.exists(), (
            "Defect A: a loop/coordinator-built ticket has no Session, so "
            "aggregate_phase_records() is empty and the gate fail-closes."
        )
        # The session must be usable as a phase-attestation home.
        session = ticket.sessions.order_by("pk").first()
        assert session is not None
        session.visit_phase("testing", agent_id="coding")
        assert ticket.aggregate_phase_records()[0] == ["testing"]

    def test_ensure_session_is_idempotent(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/998")
        first = ticket.ensure_session()
        second = ticket.ensure_session()
        assert first.pk == second.pk
        assert ticket.sessions.count() == 1

    def test_ensure_session_preserves_existing_attestation(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/997")
        existing = Session.objects.create(ticket=ticket, agent_id="coding")
        existing.visit_phase("testing", agent_id="coding")

        returned = ticket.ensure_session()

        # Must not spawn a second session that splits the ledger; the
        # existing attested one is reused.
        assert returned.pk == existing.pk
        assert ticket.aggregate_phase_records()[0] == ["testing"]
