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
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, SETTINGS, _patch_overlays


class TestProvisionRollbackPreservesAttestationSessions(TestCase):
    """A failed-provision rollback must not cascade-reap attestation.

    The Sessions holding genuine phase attestation for the ticket must
    survive the ``ticket.delete()`` rollback path (Defect B).
    """

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failed_provision_rollback_keeps_phase_sessions(self) -> None:
        # A loop-built ticket already carrying attested work: a session
        # with recorded phases (the phase ledger the gate reads).
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
            "teatree.core.management.commands._workspace_ticket_intake.WorktreeProvisioner",
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

    def test_ensure_session_reads_after_acquiring_the_row_lock(self) -> None:
        """The existence read must happen after the ticket row lock.

        Should-fix (#748 review): read+create is one atomic, row-locked
        section. The orchestrator dispatch path (`persist_agent_actions`) has no
        surrounding `transaction.atomic()`, so two concurrent loop ticks
        for the same issue_url could both see ``existing is None`` and
        both create an empty session (ledger split — contradicting the
        "never split across a fresh empty session" docstring promise).

        Modelled deterministically: a rival caller commits its session
        while *this* caller is blocked acquiring the ticket row lock
        (injected as the ``select_for_update`` side effect — exactly the
        point the loser unblocks). A correctly ordered ensure_session
        reads existence *after* the lock, sees the rival's session, and
        reuses it. The pre-fix code (read before/without the lock) misses
        the rival and creates a duplicate empty session.
        """
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/996")

        original_qs = type(Ticket.objects).select_for_update
        rival_pk: list[int] = []

        def lock_then_rival_commits(self_mgr, *args: object, **kwargs: object):
            # The competitor won the lock first and committed its
            # session; we unblock here (post-lock) and must re-read.
            if not rival_pk:
                rival_pk.append(Session.objects.create(ticket=ticket, agent_id="rival").pk)
            return original_qs(self_mgr, *args, **kwargs)

        with patch.object(type(Ticket.objects), "select_for_update", lock_then_rival_commits):
            result = ticket.ensure_session()

        # Self-explaining on regression: a non-empty rival_pk proves the
        # select_for_update lock site was actually exercised (the rival
        # was injected there). Without this, a pre-fix regression fails
        # as a bare IndexError instead of a diagnostic message.
        assert rival_pk, (
            "the select_for_update lock site was never reached — ensure_session "
            "read existence before/without the row lock (pre-fix behaviour)"
        )
        assert ticket.sessions.count() == 1, (
            f"race created {ticket.sessions.count()} sessions — ensure_session "
            "read existence before/without the row lock"
        )
        assert result.pk == rival_pk[0]
        assert ticket.ensure_session().pk == rival_pk[0]


class TestResolvePhaseSession(TestCase):
    """#801 — one canonical phase-visit session-selection policy.

    The four phase-visit writers (``ensure_session``, ``lifecycle
    visit-phase``, the ``tasks`` phase-handoff command, the ``pr`` gate)
    must all select the SAME session: the *earliest* one, under the
    ticket row lock, and never a raw blank-``agent_id`` create. This
    pins ``Ticket.resolve_phase_session`` — the single source of truth
    they all route through.
    """

    def test_selects_earliest_session_not_latest(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/801001")
        first = Session.objects.create(ticket=ticket, agent_id="coding")
        Session.objects.create(ticket=ticket, agent_id="reviewer")

        # The divergence #801 fixes: lifecycle/tasks/gate picked -pk
        # (latest); the canonical policy is earliest (matches dispatch's
        # ensure_session so attestation never splits).
        assert ticket.resolve_phase_session().pk == first.pk

    def test_creates_with_non_blank_agent_id_on_miss(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/801002")

        session = ticket.resolve_phase_session(agent_id="phase-handoff")

        assert session is not None
        # NEVER a raw blank-agent_id create (the lifecycle path's bug
        # that split attestation across a fresh empty session, #801).
        assert session.agent_id == "phase-handoff"
        assert session.agent_id.strip() != ""

    def test_default_agent_id_is_non_blank(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/801003")
        session = ticket.resolve_phase_session()
        assert session is not None
        assert session.agent_id.strip() != ""

    def test_find_phase_session_returns_none_on_miss_without_creating(self) -> None:
        # The gate path (pr.py) only READS a session; it must not create
        # one as a side effect of a gate check.
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/801004")

        session = ticket.find_phase_session()

        assert session is None
        assert ticket.sessions.count() == 0

    def test_find_phase_session_returns_earliest_existing(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/801005")
        first = Session.objects.create(ticket=ticket, agent_id="coding")
        Session.objects.create(ticket=ticket, agent_id="reviewer")

        found = ticket.find_phase_session()
        assert found is not None
        assert found.pk == first.pk

    def test_idempotent_reuses_existing_no_ledger_split(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/801006")
        existing = Session.objects.create(ticket=ticket, agent_id="coding")
        existing.visit_phase("testing", agent_id="coding")

        returned = ticket.resolve_phase_session()

        assert returned.pk == existing.pk
        assert ticket.aggregate_phase_records()[0] == ["testing"]
        assert ticket.sessions.count() == 1

    def test_ensure_session_delegates_to_resolve_phase_session(self) -> None:
        # ensure_session keeps its API/callers but is now the same
        # canonical policy (delegates) — no second selection codepath.
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/801007")
        first = Session.objects.create(ticket=ticket, agent_id="coding")
        Session.objects.create(ticket=ticket, agent_id="b")
        assert ticket.ensure_session().pk == first.pk == ticket.resolve_phase_session().pk

    def test_reads_after_acquiring_the_row_lock(self) -> None:
        """Concurrency: existence read happens AFTER the ticket row lock.

        Same leak-free in-process interleave as the ensure_session test:
        a rival commits its session at the ``select_for_update`` site;
        a correctly-ordered ``resolve_phase_session`` re-reads post-lock
        and reuses it (no duplicate empty session, no ledger split).
        Reverting the lock/earliest policy regresses this RED.
        """
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/801008")
        original_qs = type(Ticket.objects).select_for_update
        rival_pk: list[int] = []

        def lock_then_rival_commits(self_mgr, *args: object, **kwargs: object):
            if not rival_pk:
                rival_pk.append(Session.objects.create(ticket=ticket, agent_id="rival").pk)
            return original_qs(self_mgr, *args, **kwargs)

        with patch.object(type(Ticket.objects), "select_for_update", lock_then_rival_commits):
            result = ticket.resolve_phase_session()

        assert rival_pk, "select_for_update lock site never reached — read happened before/without the row lock"
        assert ticket.sessions.count() == 1, f"race created {ticket.sessions.count()} sessions"
        assert result.pk == rival_pk[0]
