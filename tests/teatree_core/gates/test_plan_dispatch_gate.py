"""Plan-before-dispatch gate: an implementing dispatch needs a recorded plan decision (#4409).

Symmetric must-refuse / must-allow, mirroring ``test_plan_gate.py``: the gate is
useless if it only ever passes, and harmful if it refuses a reviewer or a planner.
"""

from django.test import TestCase

from teatree.core.gates.plan_dispatch_gate import (
    IMPLEMENTING_PHASES,
    IMPLEMENTING_SUBAGENTS,
    PLAN_MISSING_PREFIX,
    unplanned_dispatch_refusal,
)
from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE
from teatree.core.models import Ticket
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip


def _ticket() -> Ticket:
    return Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR)


class TestImplementingPhaseSet:
    def test_derived_set_is_exactly_the_four_named_agents_phases(self) -> None:
        expected = {phase for (_role, phase), agent in SUBAGENT_BY_PHASE.items() if agent in IMPLEMENTING_SUBAGENTS}
        assert expected == IMPLEMENTING_PHASES
        assert expected == {"coding", "testing", "e2e", "debugging"}

    def test_read_only_and_coordinating_agents_are_not_implementing(self) -> None:
        assert "t3:reviewer" not in IMPLEMENTING_SUBAGENTS
        assert "t3:planner" not in IMPLEMENTING_SUBAGENTS
        assert "t3:shipper" not in IMPLEMENTING_SUBAGENTS


class TestRefusesAnUnplannedImplementingDispatch(TestCase):
    def test_every_implementing_phase_is_refused(self) -> None:
        for phase in sorted(IMPLEMENTING_PHASES):
            refusal = unplanned_dispatch_refusal(_ticket(), phase=phase)
            assert refusal is not None, f"{phase} must be refused without a plan"
            assert refusal.startswith(PLAN_MISSING_PREFIX)

    def test_refusal_names_both_remedies(self) -> None:
        ticket = _ticket()
        refusal = unplanned_dispatch_refusal(ticket, phase="coding")
        assert refusal is not None
        assert f'ticket plan {ticket.pk} "<text>"' in refusal
        assert f"ticket skip-planning {ticket.pk}" in refusal
        assert "--reason" in refusal

    def test_refusal_names_the_subagent_and_the_ticket(self) -> None:
        ticket = _ticket()
        refusal = unplanned_dispatch_refusal(ticket, phase="debugging")
        assert refusal is not None
        assert "t3:debugger" in refusal
        assert str(ticket.pk) in refusal

    def test_short_verb_spelling_normalizes_and_is_refused(self) -> None:
        # ``code``/``test`` are accepted spellings a task row can carry; a gate keyed
        # on the gerund alone would let the short verb straight through.
        assert unplanned_dispatch_refusal(_ticket(), phase="code") is not None
        assert unplanned_dispatch_refusal(_ticket(), phase="  TEST  ") is not None

    def test_a_malformed_skip_marker_is_absent_not_a_skip(self) -> None:
        ticket = _ticket()
        ticket.extra = {"trivial_plan_skip": {"reason": "   ", "by": "operator"}}
        ticket.save()
        assert unplanned_dispatch_refusal(ticket, phase="coding") is not None


class TestAllowsWhenADecisionWasRecorded(TestCase):
    def test_a_plan_artifact_satisfies_the_gate(self) -> None:
        ticket = _ticket()
        PlanArtifact.record(ticket=ticket, plan_text="Do X by Y", recorded_by="t3:planner")
        assert unplanned_dispatch_refusal(ticket, phase="coding") is None

    def test_a_trivial_skip_marker_satisfies_the_gate(self) -> None:
        ticket = _ticket()
        mark_trivial_plan_skip(ticket, reason="one-line constant bump")
        assert unplanned_dispatch_refusal(ticket, phase="coding") is None


class TestNeverRefusesANonImplementingDispatch(TestCase):
    def test_read_only_and_coordinating_phases_pass_unplanned(self) -> None:
        ticket = _ticket()
        for phase in ("planning", "reviewing", "shipping", "requesting_review", "retro", "e2e_reviewing", "bughunt"):
            assert unplanned_dispatch_refusal(ticket, phase=phase) is None, f"{phase} must never be refused"

    def test_an_unknown_phase_passes(self) -> None:
        assert unplanned_dispatch_refusal(_ticket(), phase="scanning_news") is None
