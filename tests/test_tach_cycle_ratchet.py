"""Only-shrinks ratchet for declared import cycles + core fan-in freeze (#1922).

Pins the PR-3 acyclic invariant at the *declaration* level so a regression
redeclaring a cross-module cycle, re-adding a back-edge into ``teatree.core``,
or flipping the gate off fails here instead of silently rotting (the #195 → #315
creep-back is exactly what this prevents).

Dual role with ``uv run tach check``: tach parses the actual import graph and is
the runtime cycle gate; this test guards the ``tach.toml`` declaration so a
maintainer cannot re-open the door (drop the flag, re-add the dead edge, grow
core fan-in) without a red test, even with no code edge. Both run in CI.
"""

import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TACH = _REPO / "tach.toml"

# Frozen baseline: number of modules that may depend on teatree.core.
# Re-baseline ONLY by lowering (more inversion) — never raising.
# Bumped 12 → 13 (#1993 PR7a): the reviewed cli/eval subpackage split adds
# teatree.cli.eval as a core-dependent. Same logical coupling as its parent
# teatree.cli (the eval commands were already core-dependent before the split);
# the split just promotes them to their own tach node — one new fan-in entry,
# not new coupling.
_CORE_FANIN_BASELINE = 13
_MAX_DECLARED_TWO_CYCLES = 0


def _config() -> dict:
    return tomllib.loads(_TACH.read_text(encoding="utf-8"))


def _modules() -> list[dict]:
    return _config()["modules"]


def _depends(mods: list[dict], path: str) -> list[str]:
    return next(m for m in mods if m["path"] == path).get("depends_on", [])


class TestForbidCircularStaysOn:
    def test_flag_is_true(self) -> None:
        assert _config().get("forbid_circular_dependencies") is True


class TestNoDeclaredTwoCycles:
    def test_no_mutual_edge_between_any_pair(self) -> None:
        mods = _modules()
        dep = {m["path"]: set(m.get("depends_on", [])) for m in mods}
        cycles = {tuple(sorted((a, b))) for a in dep for b in dep[a] if b in dep and a in dep[b]}
        assert len(cycles) <= _MAX_DECLARED_TWO_CYCLES, sorted(cycles)

    def test_core_does_not_depend_on_agents_or_backends(self) -> None:
        deps = set(_depends(_modules(), "teatree.core"))
        assert "teatree.agents" not in deps
        assert "teatree.backends" not in deps


class TestCoreFanInFrozen:
    def test_core_fanin_not_grown(self) -> None:
        mods = _modules()
        fanin = [m["path"] for m in mods if "teatree.core" in m.get("depends_on", []) and m["path"] != "teatree.core"]
        assert len(fanin) <= _CORE_FANIN_BASELINE, sorted(fanin)
