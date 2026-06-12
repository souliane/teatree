"""Team-role registry — the claim-namespace + overlay-seam claim filters (#1838 PR#6).

The single declarative source for the three WORK-team roles, their canonical
``team:<role>`` claim keys, and the maker roles' declarative claim filters. It
mirrors ``teatree.core.loop_lease_manager.per_loop_owner_slot``'s canonical-key
contract exactly.

The fully-qualified ``team:<role>`` form is the canonical ``claimed_by`` key.
:func:`team_claim_slot` is the single normalization seam — every claim / read /
compare qualifies a bare role UP to ``team:<role>`` at the boundary, so a bare
``core-maker`` and a qualified ``team:core-maker`` can never be treated as two
different slots. An already-qualified key is returned unchanged (idempotent).

The ``team:`` namespace is provably disjoint from the loop-owner slot
(``loop-owner``), the per-loop owner prefix (``loop:``), and the infra leases
(``loop-tick`` / ``loop-self-improve`` / … — the hyphen namespace), so a team
claim can never collide with or evict a loop owner. The two key spaces are
disjoint structurally (``team:`` vs ``loop:`` / ``loop-*``), pinned by a test.

The maker roles' overlay-seam claim filters are pure Django ``Q`` predicates —
CORE-MAKER claims ``ticket.overlay == ""`` (core units), OVERLAY-MAKER claims
``ticket.overlay != ""`` (overlay-specific units). The disjoint ``overlay`` seam
IS the context split. They are declarative data only: NOT wired into any live
claim path in this PR (the registry ships dark behind ``[teams] enabled``).
"""

from enum import Enum

from django.db.models import Q

#: Prefix for the WORK-team claim namespace. A teammate's ``claimed_by`` value
#: is ``team:<role>`` — disjoint from ``GLOBAL_OWNER_SLOT`` (``loop-owner``),
#: ``PER_LOOP_OWNER_PREFIX`` (``loop:``), and the infra-slot leases
#: (``loop-tick`` / …, which use ``-`` not ``:``). The colon-prefixed
#: ``team:`` and ``loop:`` are mutually non-prefixing, so the two key spaces
#: never overlap.
TEAM_CLAIM_PREFIX = "team:"


class TeamRole(Enum):
    """A WORK-team role: its bare slug value + its declarative claim filter.

    The enum *value* is the bare role slug; the canonical ``claimed_by`` key is
    produced only via :func:`team_claim_slot` (never hand-concatenated at a call
    site). The maker roles carry an overlay-seam :attr:`claim_filter` ``Q`` that
    partitions the backlog; REVIEWER is read-only (``role=reviewer``) and carries
    no maker claim filter (``None``).
    """

    CORE_MAKER = "core-maker"
    OVERLAY_MAKER = "overlay-maker"
    REVIEWER = "reviewer"

    @property
    def claim_filter(self) -> Q | None:
        """The declarative overlay-seam claim filter for a maker role, else ``None``.

        CORE-MAKER claims ``overlay == ""`` (core units); OVERLAY-MAKER claims
        ``overlay != ""`` (overlay-specific units). The two are a disjoint total
        cover of the backlog — the ``overlay`` seam is the context split. A pure
        ``Q`` predicate, NOT wired into any live claim path in this PR.
        """
        if self is TeamRole.CORE_MAKER:
            return Q(overlay="")
        if self is TeamRole.OVERLAY_MAKER:
            return ~Q(overlay="")
        return None


def team_claim_slot(role: "TeamRole | str") -> str:
    """Canonical ``claimed_by`` key ``team:<role>`` for a WORK-team role (#1838 PR#6).

    The single normalization seam (mirrors
    :func:`teatree.core.loop_lease_manager.per_loop_owner_slot`): a bare role
    slug (or a :class:`TeamRole`) is qualified UP to the fully-qualified
    ``team:<role>`` form at the boundary, and an already-qualified
    ``team:core-maker`` is returned unchanged (idempotent) so a call site may
    pass either form without double-prefixing. Surrounding whitespace is
    stripped. The ``team:`` namespace is disjoint from every loop-owner / infra
    slot, so a team claim never collides with a loop owner.
    """
    name = role.value if isinstance(role, TeamRole) else role.strip()
    if name.startswith(TEAM_CLAIM_PREFIX):
        return name
    return f"{TEAM_CLAIM_PREFIX}{name}"


def is_team_claim_slot(slot: str) -> bool:
    """Whether ``slot`` is a WORK-team claim key (``team:<role>``)."""
    return slot.startswith(TEAM_CLAIM_PREFIX)
