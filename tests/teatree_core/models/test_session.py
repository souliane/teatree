"""Session model tests (souliane/teatree#443 split of test_models.py)."""

import pytest
from django.test import TestCase

from teatree.core.models import QualityGateError, Session, Ticket


class TestSession(TestCase):
    def test_quality_gates_and_manual_handoff(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")

        with pytest.raises(QualityGateError, match="reviewing requires: testing"):
            session.check_gate("reviewing")

        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        session.check_gate("shipping")
        session.begin_manual_handoff()

        session.refresh_from_db()

        assert session.has_visited("testing") is True
        assert session.has_visited("reviewing") is True
        assert session.ended_at is not None
        assert str(session) == "agent-1"

    def test_shipping_gate_allows_without_retro_visit(self) -> None:
        """#837: retro is orchestrator-only — shipping no longer needs retro.

        The per-ticket shipping gate no longer requires a ``retro`` phase
        visit. ``testing`` + ``reviewing`` (recorded by distinct agents so
        maker≠checker passes) is sufficient.
        """
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing", agent_id="agent-1")
        session.visit_phase("reviewing", agent_id="agent-2")

        session.check_gate("shipping")  # must not raise — retro no longer gated

    def test_shipping_gate_still_blocks_when_reviewing_missing(self) -> None:
        """Safety: removing the retro requirement must NOT weaken the gate.

        A ticket missing ``reviewing`` is still blocked.
        """
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing")

        with pytest.raises(QualityGateError, match="shipping requires: reviewing"):
            session.check_gate("shipping")

    def test_shipping_gate_still_blocks_when_testing_missing(self) -> None:
        """Safety: a ticket missing ``testing`` is still blocked."""
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("reviewing")

        with pytest.raises(QualityGateError, match="shipping requires: testing"):
            session.check_gate("shipping")

    def test_ignores_duplicate_phase_visits_and_force_bypasses_gate(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing")
        session.visit_phase("testing")
        session.check_gate("shipping", force=True)

        assert session.visited_phases == ["testing"]

    def test_visit_phase_records_agent_id(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create(), agent_id="agent-1")

        session.visit_phase("coding", agent_id="agent-1")
        session.refresh_from_db()

        assert "coding" in session.phase_visits
        assert session.phase_visits["coding"]["agent_id"] == "agent-1"
        assert "timestamp" in session.phase_visits["coding"]

    def test_visit_phase_without_agent_id_skips_phase_visits(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("coding")
        session.refresh_from_db()

        assert session.phase_visits == {}
        assert "coding" in session.visited_phases

    def test_gate_passes_same_agent_when_phases_present(self) -> None:
        # #833: same agent_id for coding and reviewing no longer blocks —
        # independence is the reviewer-spawn boundary, not a stored id.
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing", agent_id="agent-1")
        session.visit_phase("coding", agent_id="agent-1")
        session.visit_phase("reviewing", agent_id="agent-1")
        session.visit_phase("retro", agent_id="agent-1")

        session.check_gate("shipping")  # phases present ⇒ no raise

    def test_gate_passes_without_phase_visits_attribution(self) -> None:
        # #833: an empty phase_visits audit trail does not fail closed —
        # the gate only verifies the required phases were recorded.
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing")
        session.visit_phase("coding")
        session.visit_phase("reviewing")
        session.visit_phase("retro")

        session.check_gate("shipping")  # no raise

    def test_gate_blocks_when_required_phase_missing(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing", agent_id="agent-1")
        session.visit_phase("coding", agent_id="agent-1")
        # `reviewing` and `retro` never recorded.

        with pytest.raises(QualityGateError, match="reviewing"):
            session.check_gate("shipping")

    def test_gate_bypassed_with_force(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.visit_phase("testing", agent_id="agent-1")

        session.check_gate("shipping", force=True)  # force bypasses all checks

    def test_visit_phase_normalizes_raw_spelling_at_write_boundary(self) -> None:
        """#782: ``visit_phase`` owns the canonical-phase invariant.

        A caller that passes a raw short spelling (``review``/``test`` —
        the verbs skills emit) instead of the canonical gerund must not
        corrupt the gate. Before #782 the invariant was upheld only by
        ``lifecycle.py``/``task.py`` normalizing *before* calling; any
        other caller (a new loop path, an overlay, a fixture) passing a
        raw ``review`` stored ``"review"`` verbatim, and
        ``_check_phases`` — keyed by the canonical ``reviewing`` in
        ``_REQUIRED_PHASES`` — then falsely blocked shipping with a
        stale ``requires:`` set (the #779 symptom). Enforcing
        normalization at the write boundary makes the invariant
        structural, not a caller convention.
        """
        session = Session.objects.create(ticket=Ticket.objects.create())

        # Raw short spellings, exactly as a non-normalizing caller would.
        session.visit_phase("test", agent_id="agent-1")
        session.visit_phase("review", agent_id="agent-2")
        session.refresh_from_db()

        # Stored canonical, regardless of the spelling the caller used.
        assert session.visited_phases == ["testing", "reviewing"]
        # The shipping gate must pass — not falsely block on a stale set.
        session.check_gate("shipping")

    def test_check_phases_normalizes_legacy_raw_rows_at_read_boundary(self) -> None:
        """#782: the read boundary tolerates pre-existing raw-spelling rows.

        Rows written before #782 (or by ``merge.execution`` / any path
        that bypassed ``visit_phase``) may already hold a raw ``review``.
        ``_check_phases`` must normalize membership so a legacy row still
        satisfies the canonical ``reviewing`` requirement instead of
        falsely blocking shipping forever.
        """
        session = Session.objects.create(ticket=Ticket.objects.create())

        # Simulate a legacy row written verbatim, bypassing visit_phase.
        Session.objects.filter(pk=session.pk).update(visited_phases=["test", "review"])
        session.refresh_from_db()

        session.check_gate("shipping")  # must not raise on legacy spellings

    def test_required_phases_vocabulary_cannot_drift_from_canonical(self) -> None:
        """#782: the second hand-maintained phase set stays in lockstep.

        ``Session._REQUIRED_PHASES`` is a vocabulary divorced from
        ``phases.py``. Every gate key and required phase must be a
        canonical token; the import-time guard rejects any drift so a
        typo cannot silently make the gate compare against a stale set.
        """
        from teatree.core.phases import CANONICAL_PHASES  # noqa: PLC0415

        assert Session._GATE_PHASES <= CANONICAL_PHASES

    def test_repo_tracking(self) -> None:
        session = Session.objects.create(ticket=Ticket.objects.create())

        session.mark_repo_modified("backend")
        session.mark_repo_modified("frontend")
        session.mark_repo_modified("backend")  # duplicate
        session.mark_repo_tested("backend")

        session.refresh_from_db()
        assert session.repos_modified == ["backend", "frontend"]
        assert session.repos_tested == ["backend"]
        assert session.untested_repos() == ["frontend"]
