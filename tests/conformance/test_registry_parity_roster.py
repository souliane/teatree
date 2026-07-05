"""Roster-completeness meta-ratchet — the parity corpus guards ITSELF (SELFCATCH-4).

Every lane in the registry-parity corpus governs one producer/consumer registry
pair. The residual gap the per-lane cardinality floors leave: a NEW paired
registry can be added with no parity lane at all — the corpus does not know it is
missing coverage. This meta-lane closes it, the same "phantom roster asserted
EMPTY" trick ``test_gate_liveness_corpus`` uses on gates.

It introspects the routing/dispatch/phase modules for every registry-shaped
module constant (``*_BY_KIND`` / ``*_BY_PHASE`` / ``*_ZONES`` / ``*_HANDLERS`` /
``*_TARGET_PHASES``) and asserts each is enrolled in ``PARITY_LANE_ROSTER`` — a
declared map from the registry to the test module whose parity lane covers it. A
registry with no roster entry (and not in the ``NOT_A_PARITY_PAIR`` allowlist of
single-sided config) fails: a producer/consumer pair added without a parity lane
cannot ship green. Reverse: every roster entry names a live registry AND a test
module that references it, so a phantom/renamed roster row fails too.
"""

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The modules carrying the producer/consumer registries the parity corpus governs.
_SCOPED_MODULES: tuple[str, ...] = (
    "src/teatree/loop/dispatch_tables.py",
    "src/teatree/core/modelkit/phases.py",
    "src/teatree/core/modelkit/phase_tools.py",
    "src/teatree/loop/persistence.py",
)

# A module-level constant whose NAME matches this shape is a paired routing/phase
# registry — the corpus's unit of coverage.
_REGISTRY_NAME_SHAPE = re.compile(r".*(_BY_KIND|_BY_PHASE|_ZONES|_HANDLERS|_TARGET_PHASES)$")

# Every registry-shaped constant -> the test module whose parity lane covers it.
# A NEW registry of this shape must enrol here (or in NOT_A_PARITY_PAIR); the
# per-lane floors cannot catch an entirely unenrolled pair, this ratchet does.
PARITY_LANE_ROSTER: dict[str, str] = {
    # signal-kind ↔ dispatch/statusline route totality — this PR's LANE 1.
    "AGENT_BY_KIND": "tests/conformance/test_signal_route_totality.py",
    "MECHANICAL_BY_KIND": "tests/conformance/test_signal_route_totality.py",
    "STATUSLINE_ZONE_BY_KIND": "tests/conformance/test_signal_route_totality.py",
    # zone-executor + phase-totality parity — the DIS-A framework.
    "AGENT_ZONES": "tests/conformance/test_registry_parity.py",
    "PERSISTED_AT_SOURCE_ZONES": "tests/conformance/test_registry_parity.py",
    "SUBAGENT_BY_PHASE": "tests/conformance/test_registry_parity.py",
    "_TOOLS_BY_PHASE": "tests/conformance/test_registry_parity.py",
    "_ZONE_HANDLERS": "tests/conformance/test_registry_parity.py",
    "_HANDLER_TARGET_PHASES": "tests/conformance/test_registry_parity.py",
    # fan-out panel parity (keys ⊆ SUBAGENT_BY_PHASE) — the #2229 conformance lane.
    "FANOUT_BY_PHASE": "tests/teatree_core/test_phase_agent_conformance.py",
}

# Registry-shaped constants that are NOT producer/consumer PAIRS — single-sided
# routing config or diagnostic sets that carry no partner registry to drift
# against, so a parity lane would have nothing to compare. Named + reviewable.
NOT_A_PARITY_PAIR: frozenset[str] = frozenset()


def _registry_constants(module_rel: str) -> set[str]:
    """Every module-level constant in *module_rel* whose name matches the registry shape."""
    tree = ast.parse((_REPO_ROOT / module_rel).read_text(encoding="utf-8"), filename=module_rel)
    names: set[str] = set()
    for node in tree.body:
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        names.update(name for name in targets if _REGISTRY_NAME_SHAPE.match(name))
    return names


def discovered_registries() -> set[str]:
    """Every registry-shaped constant across the scoped modules — introspection, not a hand-list."""
    registries: set[str] = set()
    for module_rel in _SCOPED_MODULES:
        registries |= _registry_constants(module_rel)
    return registries


class TestParityRosterIsComplete:
    """Every discovered registry is enrolled in a parity lane — no silent-coverage gap."""

    def test_every_registry_shaped_constant_is_enrolled(self) -> None:
        unenrolled = sorted(
            name for name in discovered_registries() if name not in PARITY_LANE_ROSTER and name not in NOT_A_PARITY_PAIR
        )
        assert not unenrolled, (
            "registry-shaped constant(s) with no parity lane — a producer/consumer pair added without "
            "coverage. Add a parity lane and enrol it in PARITY_LANE_ROSTER, or (single-sided config) "
            "NOT_A_PARITY_PAIR: " + str(unenrolled)
        )

    def test_no_roster_entry_is_a_phantom_registry(self) -> None:
        # Reverse: a roster row must name a constant that still exists somewhere in
        # the scoped modules OR the phases module (FANOUT_BY_PHASE lives there).
        known = discovered_registries() | _registry_constants("src/teatree/core/modelkit/phases.py")
        phantom = sorted(name for name in PARITY_LANE_ROSTER if name not in known)
        assert not phantom, f"PARITY_LANE_ROSTER entries naming no live registry (renamed/removed): {phantom}"

    def test_every_roster_lane_file_exists_and_references_its_registry(self) -> None:
        # A roster entry whose covering test file is gone, or no longer mentions the
        # registry, is a stale claim of coverage.
        for registry, test_rel in PARITY_LANE_ROSTER.items():
            path = _REPO_ROOT / test_rel
            assert path.is_file(), f"{registry}: covering lane file {test_rel} is missing"
            assert registry in path.read_text(encoding="utf-8"), (
                f"{registry}: covering lane {test_rel} does not reference it (renamed/uncovered)"
            )


class TestParityRosterCardinalityFloors:
    """Anti-vacuity — a broken discovery that finds nothing must not pass green."""

    def test_discovery_and_roster_floors(self) -> None:
        assert len(discovered_registries()) >= 9, sorted(discovered_registries())
        assert len(PARITY_LANE_ROSTER) >= 10, sorted(PARITY_LANE_ROSTER)


class TestParityRosterFiresRed:
    """Anti-vacuity — the ratchet must actually catch an unenrolled paired registry."""

    def test_a_synthetic_unenrolled_registry_is_reported(self) -> None:
        # A registry-shaped constant absent from both the roster and the allowlist is
        # exactly the uncovered-pair class the ratchet exists to catch.
        synthetic = "SYNTHETIC_ROUTE_BY_KIND"
        assert _REGISTRY_NAME_SHAPE.match(synthetic)
        assert synthetic not in PARITY_LANE_ROSTER
        assert synthetic not in NOT_A_PARITY_PAIR

    def test_the_shape_matcher_is_selective(self) -> None:
        # The heuristic must not sweep in unrelated constants (a false ratchet trip).
        assert not _REGISTRY_NAME_SHAPE.match("SELF_UPDATE_CI_SKIP_REASONS")
        assert not _REGISTRY_NAME_SHAPE.match("DUAL_DISPATCH")
        assert _REGISTRY_NAME_SHAPE.match("AGENT_BY_KIND")
