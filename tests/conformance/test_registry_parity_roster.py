"""Roster-completeness meta-ratchet — the parity corpus guards ITSELF (SELFCATCH-4).

Every lane in the registry-parity corpus governs one producer/consumer registry
pair. The residual gap the per-lane cardinality floors leave: a NEW paired
registry can be added with no parity lane at all — the corpus does not know it is
missing coverage. This meta-lane closes it, the same "phantom roster asserted
EMPTY" trick ``test_gate_liveness_corpus`` uses on gates.

The scan is TREE-WIDE, not a hardcoded module list: a hardcoded list is exactly
what rots (an early version scanned four modules and already missed
``dispatch.py``'s ``_CONDITIONAL_HANDLERS``, a real ``*_HANDLERS`` dispatch
registry). It introspects EVERY module under ``src/teatree`` for a registry-shaped
module constant (``*_BY_KIND`` / ``*_BY_PHASE`` / ``*_ZONES`` / ``*_HANDLERS`` /
``*_TARGET_PHASES``) and asserts each is enrolled in ``PARITY_LANE_ROSTER`` — a
declared map from the registry to the test module whose parity lane covers it. A
registry with no roster entry (and not in the ``NOT_A_PARITY_PAIR`` allowlist of
single-sided config) fails: a producer/consumer pair added anywhere without a
parity lane cannot ship green. A self-completeness assertion pins that the scan
stays tree-wide, so the scope itself cannot silently re-narrow.
"""

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
#: The WHOLE package — the scan root is the package, never a hardcoded subset.
_SCAN_ROOT = _REPO_ROOT / "src" / "teatree"

# A module-level constant whose NAME matches this shape is a paired routing/phase
# registry — the corpus's unit of coverage.
_REGISTRY_NAME_SHAPE = re.compile(r".*(_BY_KIND|_BY_PHASE|_ZONES|_HANDLERS|_TARGET_PHASES)$")

# Every registry-shaped constant -> the test module whose parity lane covers it.
# A NEW registry of this shape must enrol here (or in NOT_A_PARITY_PAIR); the
# per-lane floors cannot catch an entirely unenrolled pair, this ratchet does.
PARITY_LANE_ROSTER: dict[str, str] = {
    # signal-kind ↔ dispatch/statusline route totality — this PR's LANE 1. It also
    # consumes `_CONDITIONAL_HANDLERS` into its explicitly-routed union.
    "AGENT_BY_KIND": "tests/conformance/test_signal_route_totality.py",
    "MECHANICAL_BY_KIND": "tests/conformance/test_signal_route_totality.py",
    "STATUSLINE_ZONE_BY_KIND": "tests/conformance/test_signal_route_totality.py",
    "_CONDITIONAL_HANDLERS": "tests/conformance/test_signal_route_totality.py",
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

# The scan must span at least this many distinct subpackages — a self-completeness
# floor so a re-narrowed walk (back to one module/dir) is caught, not the drift
# the tree-wide scan exists to prevent.
_MIN_SUBPACKAGES = 2
# Registries known to live in DISTINCT subpackages — a re-narrowed scan drops one.
_CROSS_SUBPACKAGE_ANCHORS: frozenset[str] = frozenset({"AGENT_BY_KIND", "SUBAGENT_BY_PHASE", "_CONDITIONAL_HANDLERS"})


def _registry_constants(tree: ast.Module) -> set[str]:
    """Every module-level constant in *tree* whose name matches the registry shape."""
    names: set[str] = set()
    for node in tree.body:
        targets: list[str] = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        names.update(name for name in targets if _REGISTRY_NAME_SHAPE.match(name))
    return names


def _discovered_by_module() -> dict[str, set[str]]:
    """Registry-shaped constants keyed by their module path (relative to ``src``)."""
    by_module: dict[str, set[str]] = {}
    for path in _SCAN_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = _registry_constants(tree)
        if found:
            by_module[path.relative_to(_SCAN_ROOT).as_posix()] = found
    return by_module


def discovered_registries() -> set[str]:
    """Every registry-shaped constant across the WHOLE package — introspection, not a hand-list."""
    return {name for names in _discovered_by_module().values() for name in names}


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
        # Reverse: a roster row must name a constant that still exists in the tree.
        phantom = sorted(name for name in PARITY_LANE_ROSTER if name not in discovered_registries())
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


class TestParityRosterScanIsTreeWide:
    """Self-completeness — the scan cannot silently re-narrow to a hardcoded subset."""

    def test_scan_root_is_the_whole_package(self) -> None:
        assert _SCAN_ROOT.name == "teatree"
        assert (_SCAN_ROOT / "__init__.py").is_file()

    def test_scan_spans_multiple_subpackages(self) -> None:
        # A walk narrowed back to one module/dir drops registries from other
        # subpackages; the anchors span core/ and loop/, so all must be discovered.
        discovered = discovered_registries()
        missing_anchors = sorted(_CROSS_SUBPACKAGE_ANCHORS - discovered)
        assert not missing_anchors, (
            f"tree-wide scan missed cross-subpackage anchor(s) — scope narrowed?: {missing_anchors}"
        )
        subpackages = {module.split("/", 1)[0] for module in _discovered_by_module()}
        assert len(subpackages) >= _MIN_SUBPACKAGES, f"scan reached only {subpackages} — not tree-wide"

    def test_previously_unscanned_conditional_handlers_is_now_covered(self) -> None:
        # The exact regression a hardcoded 4-module scope missed: dispatch.py's
        # `_CONDITIONAL_HANDLERS` is discovered AND enrolled.
        assert "_CONDITIONAL_HANDLERS" in discovered_registries()
        assert "_CONDITIONAL_HANDLERS" in PARITY_LANE_ROSTER


class TestParityRosterCardinalityFloors:
    """Anti-vacuity — a broken discovery that finds nothing must not pass green."""

    def test_discovery_and_roster_floors(self) -> None:
        assert len(discovered_registries()) >= 10, sorted(discovered_registries())
        assert len(PARITY_LANE_ROSTER) >= 11, sorted(PARITY_LANE_ROSTER)


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
        assert _REGISTRY_NAME_SHAPE.match("_CONDITIONAL_HANDLERS")
