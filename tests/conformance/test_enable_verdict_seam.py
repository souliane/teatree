"""Nothing computes an enable decision outside the one seam — an AST guard, not a convention.

#4185 shipped because two call sites resolved the loop mask separately and were kept in
step by discipline; #4196 is the same failure one level up, between chain membership and
the tick. Discipline is not the fix — a mechanical one is. This walks the tree and refuses
a THIRD variant of the verdict rather than waiting for the next incident to find it.

One rule, with an explicit allowlist naming why each exception is not a variant: only
:mod:`teatree.loops.enable_verdict` combines the planes
(:func:`teatree.loop.loop_state_db.loop_state_admits`).

Plus the preset-layer guard: the override/schedule resolver answers "is an override or a
schedule slot governing", NOT "does this loop run". Reading it for an enable decision is
exactly the defect — it cannot see the configured default mode.

There is no longer a SECOND, wider reading to guard. Membership and admission were kept
apart only because the live-presence upgrade could flip with no event to hook; with that
arm gone they are one set, so the closure and its one-caller rule are both deleted rather
than left as a guard over nothing.
"""

import ast
from pathlib import Path

from tests.conformance._src_tree import REPO_ROOT, src_modules

_SEAM_MODULE = "src/teatree/loops/enable_verdict.py"

#: The plane-combining predicate. Called only where the planes are held together.
_COMBINER = "loop_state_admits"


#: The L3/L2 preset resolver's enable-shaped names.
_PRESET_LAYER_NAMES = frozenset({"resolve_active_preset", "preset_state_for", "resolve_preset_state"})

#: Modules that legitimately read the preset layer, each for a NON-enable question.
_PRESET_LAYER_ALLOWED = {
    # The layer itself — a definition site is not a reader.
    "src/teatree/loop/preset_resolution.py",
    # The producer: the merged mode resolver is built ON the preset layer.
    "src/teatree/core/mode_resolution.py",
    # Reports WHICH preset governs (override vs schedule) for `preset show` / the
    # statusline handle — a posture readout, never a per-loop run decision.
    "src/teatree/loops/preset_status.py",
    # Stamps the preset layer to drive the owner-facing drain + Slack line. Its chain
    # reconcile is keyed on the resolved MODE instead, precisely because this layer
    # cannot see an L0 default_mode change.
    "src/teatree/loops/preset_transitions.py",
}


def _called_names(tree: ast.AST) -> set[str]:
    return {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    }


def _imported_names(tree: ast.AST) -> set[str]:
    return {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}


def _src_relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


class TestOnlyTheSeamCombinesThePlanes:
    def test_no_module_outside_the_seam_calls_the_plane_combiner(self) -> None:
        offenders = sorted(
            _src_relative(path)
            for path, tree in src_modules()
            if _src_relative(path) != _SEAM_MODULE and _COMBINER in _called_names(tree)
        )
        assert offenders == [], (
            f"{_COMBINER} is called outside {_SEAM_MODULE}: {offenders}. Combining the planes "
            "in a second place is how membership and admission drifted apart (#4196) — ask "
            "EnablePlanes.admits (instant) or membership_loop_names (persisted) instead."
        )


class TestThePresetLayerIsNotAnEnableDecision:
    def test_no_new_module_reads_the_preset_layer(self) -> None:
        offenders = sorted(
            _src_relative(path)
            for path, tree in src_modules()
            if _src_relative(path) not in _PRESET_LAYER_ALLOWED
            and (_PRESET_LAYER_NAMES & (_imported_names(tree) | _called_names(tree)))
        )
        assert offenders == [], (
            f"the L3/L2 preset resolver answers 'which preset governs', not 'does this loop "
            f"run' — it is blind to the L0 default mode and the live-presence upgrade, which "
            f"is the #4196 defect. Read teatree.loops.enable_verdict instead. Offenders: "
            f"{offenders}"
        )

    def test_the_allowlist_names_only_live_modules(self) -> None:
        # An allowlist entry whose module is gone would silently widen the guard.
        live = {_src_relative(path) for path, _ in src_modules()}
        assert live >= _PRESET_LAYER_ALLOWED, sorted(_PRESET_LAYER_ALLOWED - live)
