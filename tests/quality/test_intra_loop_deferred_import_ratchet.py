"""Shrink-ratchet for intra-``teatree.loop`` deferred (function-scoped) imports (#2413).

``teatree.loop`` is one tach node holding ~109 .py files across ``scanners`` /
``self_improve`` / ``slack_answer`` / ``phases`` plus the flat ``tick`` /
``dispatch`` / ``statusline`` / ``rendering`` clusters. Inside a single node
tach's acyclic guard cannot see intra-node cycles, so the back-edges that route
``statusline`` / ``dispatch`` / ``scanners`` back up into the orchestration top
(``tick``, ``queue_drain``, ``review_claim``, ``loop_scoping``) are hidden in
**function-scoped** (deferred) imports — invisible to tach, invisible to the
human reading the module header. The #2413 sub-layering (mirroring the #2385
core carve) declares the lowest leaves as their own tach nodes so the acyclic
guard applies *within* ``loop``; until that carve is complete (PR-2 declares
``scanners`` and severs the ``review_claim`` back-edges, PR-3/PR-4 the rest),
this ratchet keeps the deferred-import count from growing.

It is a SHRINK-only peg: an AST walk counts every ``import``/``from`` whose
enclosing scope is a function/method (not module-level) and whose target module
starts with ``teatree.loop``. ``current <= _FROZEN`` blocks growth; on
``current < _FROZEN`` the message instructs lowering the peg so the gain is
banked (mirroring ``test_intra_core_deferred_import_ratchet.py``).

The companion ``TestLoopStatuslineLeaf`` pins the PR-1 carve itself: the
``statusline_palette`` / ``statusline_render`` pair is declared as the lowest
``domain`` leaf (``render`` depends only on ``palette``; ``palette`` is a true
``depends_on = []`` leaf), and the parent ``teatree.loop`` declares both
children. Only this pair is declarable in PR-1 — the public ``statusline``
module reaches into ``statusline_loops``, which defers up-edges into
``tick_piggyback`` / ``queue_drain`` / ``loop_scoping`` (the orchestration top),
so the full ``statusline`` cluster carries an unresolvable up-edge until PR-2+
sever it. ``dispatch`` is likewise deferred to PR-2: every ``dispatch*`` module
imports ``teatree.loop.scanners.base`` eagerly, and ``scanners`` is not a
declared node until PR-2.
"""

import ast
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOOP_ROOT = _REPO_ROOT / "src" / "teatree" / "loop"
_TACH = _REPO_ROOT / "tach.toml"

# Measured in-worktree at the #2413 PR-1 base (origin/main @ ac627c31c): the
# count of function-scoped imports under `src/teatree/loop/` whose target starts
# with `teatree.loop`. The plan's "175" was the repo-wide PLC0415-noqa
# total (deferred imports to *any* target); this ratchet — mirroring the core
# one — counts only the *intra-loop* deferred edges that hide a loop-internal
# cycle from tach, which is 42. PR-1 is leaf-carve only and moves no module, so
# the count is unchanged by the carve.
# SHRINK-ONLY: lower this as PR-2+ convert deferred edges into declared tach
# sub-node edges; never raise it.
_FROZEN_INTRA_LOOP_DEFERRED = 42


def _function_scoped_intra_loop_imports(source: Path) -> int:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def in_function_scope(node: ast.AST) -> bool:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.FunctionDef | ast.AsyncFunctionDef):
                return True
            cur = parents.get(cur)
        return False

    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("teatree.loop") and in_function_scope(node):
                count += 1
        elif isinstance(node, ast.Import):
            count += sum(1 for alias in node.names if alias.name.startswith("teatree.loop") and in_function_scope(node))
    return count


def _count_all() -> int:
    return sum(_function_scoped_intra_loop_imports(py) for py in sorted(_LOOP_ROOT.rglob("*.py")))


def _module_entry(path: str) -> dict[str, object]:
    data = tomllib.loads(_TACH.read_text(encoding="utf-8"))
    return next(m for m in data["modules"] if m["path"] == path)


class TestIntraLoopDeferredImportRatchet:
    def test_count_does_not_exceed_frozen(self) -> None:
        current = _count_all()
        assert current <= _FROZEN_INTRA_LOOP_DEFERRED, (
            f"intra-teatree.loop deferred (function-scoped) imports grew to {current}, "
            f"over the frozen ceiling {_FROZEN_INTRA_LOOP_DEFERRED}. A new function-scoped "
            f"`from teatree.loop... import` hides an intra-loop edge from tach's acyclic "
            f"guard. Make the edge a declared tach sub-node edge (#2413) instead of "
            f"adding another deferred import."
        )

    def test_count_is_not_below_frozen(self) -> None:
        # Banks every reduction immediately: when an edge becomes a declared tach
        # sub-node edge, the count drops and the peg must follow it down so a
        # future regression cannot silently spend the gain.
        current = _count_all()
        assert current >= _FROZEN_INTRA_LOOP_DEFERRED, (
            f"intra-teatree.loop deferred imports dropped to {current}, below the frozen "
            f"ceiling {_FROZEN_INTRA_LOOP_DEFERRED}. Lower _FROZEN_INTRA_LOOP_DEFERRED to "
            f"{current} to bank the reduction."
        )


class TestLoopStatuslineLeaf:
    def test_statusline_palette_is_a_pure_leaf_node(self) -> None:
        entry = _module_entry("teatree.loop.statusline_palette")
        assert entry["layer"] == "domain"
        assert entry.get("depends_on", []) == []

    def test_statusline_render_depends_only_on_palette(self) -> None:
        entry = _module_entry("teatree.loop.statusline_render")
        assert entry["layer"] == "domain"
        assert entry.get("depends_on", []) == ["teatree.loop.statusline_palette"]

    def test_parent_loop_declares_statusline_leaf_children(self) -> None:
        parent = _module_entry("teatree.loop")
        deps = parent.get("depends_on", [])
        assert "teatree.loop.statusline_palette" in deps
        assert "teatree.loop.statusline_render" in deps
