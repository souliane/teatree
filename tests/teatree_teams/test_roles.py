"""Inert team-role registry — the claim-namespace + overlay-seam claim filters (#1838 PR#6).

The WORK-team (Track B) declares three roles — CORE_MAKER, OVERLAY_MAKER,
REVIEWER — each with a canonical ``claimed_by`` key in the ``team:<role>``
namespace (disjoint from the loop-owner / per-loop / infra slots), and each
maker role a declarative overlay-seam claim filter (CORE → ``overlay == ""``,
OVERLAY → ``overlay != ""``). This PR ships the registry DARK: nothing in the
loop / dispatch / claim path references it yet (see ``test_inert.py``).
"""

import pytest

from teatree.core.loop_lease_manager import GLOBAL_OWNER_SLOT, PER_LOOP_OWNER_PREFIX, per_loop_owner_slot
from teatree.core.models import Ticket
from teatree.teams.roles import TEAM_CLAIM_PREFIX, TeamRole, is_team_claim_slot, team_claim_slot
from tests.factories import TicketFactory


class TestTeamClaimSlot:
    """``team_claim_slot`` mirrors ``per_loop_owner_slot``'s canonical-key contract."""

    def test_qualifies_a_bare_role_up_to_team_namespace(self) -> None:
        assert team_claim_slot("core-maker") == "team:core-maker"
        assert team_claim_slot("reviewer") == "team:reviewer"

    def test_accepts_a_team_role_enum(self) -> None:
        assert team_claim_slot(TeamRole.CORE_MAKER) == "team:core-maker"
        assert team_claim_slot(TeamRole.OVERLAY_MAKER) == "team:overlay-maker"
        assert team_claim_slot(TeamRole.REVIEWER) == "team:reviewer"

    def test_already_qualified_is_returned_unchanged(self) -> None:
        # Idempotent: a caller may pass either the bare role or the qualified
        # slot without double-prefixing (mirrors per_loop_owner_slot).
        assert team_claim_slot("team:core-maker") == "team:core-maker"
        assert team_claim_slot(team_claim_slot(TeamRole.REVIEWER)) == "team:reviewer"

    def test_strips_surrounding_whitespace(self) -> None:
        assert team_claim_slot("  overlay-maker  ") == "team:overlay-maker"

    def test_every_role_round_trips(self) -> None:
        for role in TeamRole:
            slot = team_claim_slot(role)
            assert slot.startswith(TEAM_CLAIM_PREFIX)
            assert is_team_claim_slot(slot)
            # Re-qualifying the slot is a no-op.
            assert team_claim_slot(slot) == slot


class TestIsTeamClaimSlot:
    def test_true_for_team_keys(self) -> None:
        assert is_team_claim_slot("team:core-maker") is True
        assert is_team_claim_slot(team_claim_slot(TeamRole.REVIEWER)) is True

    def test_false_for_non_team_keys(self) -> None:
        assert is_team_claim_slot(GLOBAL_OWNER_SLOT) is False
        assert is_team_claim_slot(per_loop_owner_slot("dispatch")) is False
        assert is_team_claim_slot("loop-tick") is False
        assert is_team_claim_slot("") is False


class TestDisjointness:
    """``team:<role>`` keys are provably disjoint from every owner/infra slot."""

    def test_team_prefix_differs_from_loop_prefixes(self) -> None:
        assert TEAM_CLAIM_PREFIX == "team:"
        assert not TEAM_CLAIM_PREFIX.startswith(PER_LOOP_OWNER_PREFIX)
        assert not PER_LOOP_OWNER_PREFIX.startswith(TEAM_CLAIM_PREFIX)

    def test_no_team_slot_collides_with_the_global_owner_slot(self) -> None:
        for role in TeamRole:
            assert team_claim_slot(role) != GLOBAL_OWNER_SLOT

    def test_no_team_slot_collides_with_a_per_loop_owner_slot(self) -> None:
        # team:<role> is never produced by per_loop_owner_slot and vice versa.
        for role in TeamRole:
            slot = team_claim_slot(role)
            assert not slot.startswith(PER_LOOP_OWNER_PREFIX)
            assert per_loop_owner_slot(role.value) != slot

    @pytest.mark.parametrize("infra_slot", ["loop-tick", "loop-self-improve", "loop-owner"])
    def test_no_team_slot_collides_with_an_infra_slot(self, infra_slot: str) -> None:
        # Infra leases use the hyphen namespace (`loop-*`), never the colon.
        for role in TeamRole:
            assert team_claim_slot(role) != infra_slot


class TestTeamRoleEnum:
    def test_three_roles_exist(self) -> None:
        assert {role.name for role in TeamRole} == {"CORE_MAKER", "OVERLAY_MAKER", "REVIEWER"}

    def test_role_values_are_the_bare_slugs(self) -> None:
        assert TeamRole.CORE_MAKER.value == "core-maker"
        assert TeamRole.OVERLAY_MAKER.value == "overlay-maker"
        assert TeamRole.REVIEWER.value == "reviewer"

    def test_maker_roles_carry_a_claim_filter(self) -> None:
        assert TeamRole.CORE_MAKER.claim_filter is not None
        assert TeamRole.OVERLAY_MAKER.claim_filter is not None

    def test_reviewer_has_no_maker_claim_filter(self) -> None:
        # REVIEWER is read-only (role=reviewer), not an overlay-seam maker.
        assert TeamRole.REVIEWER.claim_filter is None


@pytest.mark.django_db
class TestClaimFilterPartition:
    """The maker claim filters partition the backlog along the overlay seam."""

    def _make_tickets(self) -> None:
        TicketFactory(overlay="")
        TicketFactory(overlay="")
        TicketFactory(overlay="t3-teatree")
        TicketFactory(overlay="some-overlay")

    def test_core_maker_selects_only_core_tickets(self) -> None:
        self._make_tickets()
        selected = Ticket.objects.filter(TeamRole.CORE_MAKER.claim_filter)
        assert selected.count() == 2
        assert all(t.overlay == "" for t in selected)

    def test_overlay_maker_selects_only_overlay_tickets(self) -> None:
        self._make_tickets()
        selected = Ticket.objects.filter(TeamRole.OVERLAY_MAKER.claim_filter)
        assert selected.count() == 2
        assert all(t.overlay != "" for t in selected)

    def test_the_two_maker_filters_are_a_disjoint_total_cover(self) -> None:
        self._make_tickets()
        core = set(Ticket.objects.filter(TeamRole.CORE_MAKER.claim_filter).values_list("pk", flat=True))
        overlay = set(Ticket.objects.filter(TeamRole.OVERLAY_MAKER.claim_filter).values_list("pk", flat=True))
        everything = set(Ticket.objects.values_list("pk", flat=True))
        assert core.isdisjoint(overlay)
        assert core | overlay == everything
