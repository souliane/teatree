# test-path: cross-cutting
"""Every size/count ratchet in this tree is ONE-SIDED: growth reds, improvement passes.

A ratchet exists to stop a number climbing. When it is written as an equality
(or paired with an under-peg assertion) it also stops the number FALLING, and
CI turns red on the commit that deletes a module, severs a deferred import, or
resolves an unfinished statement. That is a mechanical tax on improvement, and
it is why the ledgers this repo keeps only ever grew.

This module is the direction contract. For each ratchet it drives the ratchet's
OWN predicate — the same counting and comparison code the live gate runs — over
a synthetic input, and pins both halves:

* a simulated REGROWTH is still refused (the ratchet keeps its teeth), and
* a simulated SHRINK is accepted (improvement is free).

Driving the real predicate over a synthetic tree is what makes this
non-vacuous: asserting ``pin + 1 > pin`` would restate the operator rather than
exercise the gate. The three primary boundaries sit at exactly zero slack, so
the regrowth half is the load-bearing one — the next line added in any of them
must still fire.
"""

from pathlib import Path

import tests.test_hook_router_size_gate as router_gate
from tests.quality._deferred_imports import PegDrift, diff_pegs
from tests.quality.test_no_flat_core_regrowth import PINNED_FLAT_CORE_MODULES, exceeds_ceiling, flat_core_modules


def _core_tree(root: Path, leaves: int) -> Path:
    """A synthetic ``core/`` holding *leaves* flat leaf modules plus the excluded shapes."""
    core = root / "core"
    (core / "subpackage").mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "subpackage" / "__init__.py").write_text("", encoding="utf-8")
    (core / "subpackage" / "inside.py").write_text("", encoding="utf-8")
    for index in range(leaves):
        (core / f"leaf_{index:03d}.py").write_text("", encoding="utf-8")
    return core


class TestFlatCoreLeafRatchet:
    """``tests/quality/test_no_flat_core_regrowth.py`` — the only directory-count pin."""

    def test_counts_flat_leaves_only(self, tmp_path: Path) -> None:
        # The synthetic tree is only evidence if the walker reads it the way it
        # reads the live one: leaves at the root, never __init__ or a subpackage.
        assert flat_core_modules(_core_tree(tmp_path, 3)) == ["leaf_000.py", "leaf_001.py", "leaf_002.py"]

    def test_regrowth_past_the_pin_is_refused(self, tmp_path: Path) -> None:
        core = _core_tree(tmp_path, PINNED_FLAT_CORE_MODULES + 1)
        assert exceeds_ceiling(core, PINNED_FLAT_CORE_MODULES), (
            "a leaf added at the flat core root must still fire the ratchet — this is the "
            "regression the ceiling exists for, and the live tree sits at exactly zero slack"
        )

    def test_shrink_below_the_pin_is_accepted(self, tmp_path: Path) -> None:
        core = _core_tree(tmp_path, PINNED_FLAT_CORE_MODULES - 1)
        assert not exceeds_ceiling(core, PINNED_FLAT_CORE_MODULES), (
            "relocating a leaf into the subpackage that owns its concern must not turn CI red — "
            "the ceiling is one-sided, not an equality"
        )


class TestPerFilePegRatchets:
    """The shared per-file peg ledger behind the deferred-import and marker ratchets."""

    def test_growth_over_a_peg_is_refused(self) -> None:
        drift = diff_pegs({"src/a.py": 3}, {"src/a.py": 2})
        assert [path for path, _live, _peg in drift.over_peg] == ["src/a.py"]

    def test_an_unlisted_file_pegs_at_zero(self) -> None:
        drift = diff_pegs({"src/new.py": 1}, {})
        assert [path for path, _live, _peg in drift.over_peg] == ["src/new.py"]

    def test_shrink_below_a_peg_is_accepted(self) -> None:
        assert diff_pegs({"src/a.py": 1}, {"src/a.py": 3}).over_peg == ()

    def test_a_file_that_drops_to_zero_is_accepted(self) -> None:
        assert diff_pegs({}, {"src/gone.py": 2}).over_peg == ()

    def test_the_ledger_exposes_no_under_peg_surface(self) -> None:
        # The direction is a property of the DATA SHAPE, not of which assertions
        # happen to be written today: with no under-peg field there is nothing a
        # future ratchet can assert on, so the improvement tax cannot come back
        # by someone re-adding a test.
        assert not hasattr(PegDrift, "under_peg")
        assert not hasattr(PegDrift, "under_lines")


class TestHookRouterCeiling:
    """``tests/test_hook_router_size_gate.py`` — the shrink-only router ceiling."""

    @staticmethod
    def _router_text(loc: int) -> str:
        return "\n".join(["# a comment line is not counted", "", *[f"line_{i} = {i}" for i in range(loc)]])

    def test_counts_code_lines_only(self) -> None:
        assert router_gate._count_loc(self._router_text(7)) == 7

    def test_growth_past_the_ceiling_is_refused(self) -> None:
        assert router_gate.over_ceiling(self._router_text(router_gate._CEILING_LOC + 1)), (
            "a handler registered in the router body instead of a sibling module must still fire — "
            "the live router sits at exactly the ceiling"
        )

    def test_a_large_shrink_is_accepted(self) -> None:
        assert not router_gate.over_ceiling(self._router_text(router_gate._CEILING_LOC - 200))

    def test_the_gate_asserts_no_upper_bound_on_slack(self) -> None:
        # The tightness assertion (`slack <= 25`) made a >25-LOC extraction red
        # until _CEILING_LOC was hand-lowered in the same commit. Its absence is
        # the contract: growth is caught by over_ceiling here and, for an
        # over-cap file, by check_module_health's from-ref shrink-only rule.
        assert not hasattr(router_gate, "test_ceiling_is_kept_tight_so_the_gate_has_teeth")
