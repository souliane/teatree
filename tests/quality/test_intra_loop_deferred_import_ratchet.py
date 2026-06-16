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
``scanners`` and severs the ``review_claim`` back-edges, PR-3 declares the
eager-clean ``dispatch`` flat-file cluster + the ``self_improve`` detector
package, PR-4 the ``rendering`` + ``tick`` / ``phases`` top once the
``statusline_loops`` deferred up-edges are severed), this ratchet keeps the
deferred-import count from growing.

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

# Measured in-worktree: the count of function-scoped imports under
# `src/teatree/loop/` whose target starts with `teatree.loop`. The plan's "175"
# was the repo-wide PLC0415-noqa total (deferred imports to *any* target); this
# ratchet — mirroring the core one — counts only the *intra-loop* deferred edges
# that hide a loop-internal cycle from tach. PR-1 (leaf-carve only) measured 42.
# PR-2 declares `teatree.loop.scanners` (+ the `review_claim_signals` /
# `url_specificity` / `review_request_tracker` / `dispatch_tables` /
# `pr_ticket_index` supporting leaves) and converts six deferred edges into
# declared eager sub-node edges (2x `review_loop_enabled`, 2x
# `best_url_match_specificity`, `record_review_request_post`,
# `resolve_author_ticket`), dropping the count 42 -> 36.
# PR-3 declares the `dispatch` flat-file cluster (`dispatch` / `dispatch_gates` /
# `dispatch_reducer`) and the `self_improve` package as tach nodes. Both were
# already eager-clean (every intra-loop edge pointed DOWN, zero deferred
# up-edges), so the carve converts NO deferred edge — the count stays 36. The
# win is structural: tach's acyclic guard now applies WITHIN those clusters.
# SHRINK-ONLY: lower this as PR-4 converts the `statusline_loops` deferred
# up-edges into declared tach sub-node edges; never raise it.
_FROZEN_INTRA_LOOP_DEFERRED = 36


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


def _depends_on(path: str) -> list[str]:
    raw = _module_entry(path).get("depends_on", [])
    assert isinstance(raw, list)
    return [str(dep) for dep in raw]


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


_SCANNERS_ROOT = _LOOP_ROOT / "scanners"


def _eager_loop_imports(source: Path) -> set[str]:
    """Module-level (non-function-scoped) ``teatree.loop...`` import targets."""
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

    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if (node.module or "").startswith("teatree.loop") and not in_function_scope(node):
            out.add(node.module or "")
    return out


class TestLoopScannersNode:
    """``teatree.loop.scanners`` is a declared domain node (#2413 PR-2).

    The ``review_claim`` back-edges are severed and the cycle is structurally
    forbidden by tach, not merely deferred out of sight.
    """

    def test_scanners_is_a_domain_node(self) -> None:
        entry = _module_entry("teatree.loop.scanners")
        assert entry["layer"] == "domain"

    def test_scanners_depends_on_the_review_claim_signals_leaf_not_review_claim(self) -> None:
        deps = _module_entry("teatree.loop.scanners").get("depends_on", [])
        assert "teatree.loop.review_claim_signals" in deps
        # The orchestration-top `teatree.loop.review_claim` is NEVER a scanners dep —
        # that edge is the cycle PR-2 severs.
        assert "teatree.loop.review_claim" not in deps
        assert "teatree.loop" not in deps

    def test_review_claim_signals_is_a_clean_leaf(self) -> None:
        entry = _module_entry("teatree.loop.review_claim_signals")
        assert entry["layer"] == "domain"
        assert set(entry.get("depends_on", [])) == {"teatree.types", "teatree.core.models"}

    def test_url_specificity_is_a_pure_leaf(self) -> None:
        entry = _module_entry("teatree.loop.url_specificity")
        assert entry["layer"] == "domain"
        assert entry.get("depends_on", []) == []

    def test_parent_loop_declares_the_new_leaf_children(self) -> None:
        deps = _module_entry("teatree.loop").get("depends_on", [])
        for child in (
            "teatree.loop.scanners",
            "teatree.loop.review_claim_signals",
            "teatree.loop.url_specificity",
            "teatree.loop.review_request_tracker",
            "teatree.loop.dispatch_tables",
            "teatree.loop.pr_ticket_index",
        ):
            assert child in deps, child

    def test_no_scanner_eagerly_imports_review_claim(self) -> None:
        # The two severed back-edges (`slack_broadcasts` / `slack_review_intent`)
        # must never re-import the orchestration-top `review_claim` module — they
        # reach the discovery primitives via the `review_claim_signals` leaf.
        offenders = sorted(
            str(py.relative_to(_SCANNERS_ROOT))
            for py in _SCANNERS_ROOT.rglob("*.py")
            if "teatree.loop.review_claim" in _eager_loop_imports(py)
        )
        assert offenders == [], f"scanners eagerly importing orchestration-top review_claim: {offenders}"


class TestLoopDispatchNode:
    """The ``dispatch`` flat-file cluster is declared as tach domain nodes (#2413 PR-3).

    The cluster (``dispatch`` > ``dispatch_gates`` > ``dispatch_reducer`` >
    ``dispatch_tables``) was eager-clean — every intra-loop edge already pointed
    DOWN (to ``scanners`` / ``dispatch_tables`` / ``review_claim_signals``) with
    zero deferred up-edges — but lived inside the single ``teatree.loop`` node,
    so tach's acyclic guard could not see it. Declaring each flat file as its own
    ``domain`` node makes the within-cluster DAG enforced: a future back-edge
    (e.g. ``dispatch_reducer`` importing ``dispatch`` or the orchestration top)
    is now a tach failure, not an invisible cycle.
    """

    def test_dispatch_cluster_files_are_domain_nodes(self) -> None:
        for path in (
            "teatree.loop.dispatch",
            "teatree.loop.dispatch_gates",
            "teatree.loop.dispatch_reducer",
        ):
            assert _module_entry(path)["layer"] == "domain", path

    def test_dispatch_never_depends_on_the_orchestration_top(self) -> None:
        # No file in the dispatch cluster may declare a dependency on the
        # orchestration-top `teatree.loop` (the parent) — that is the back-edge
        # the carve forbids structurally.
        for path in (
            "teatree.loop.dispatch",
            "teatree.loop.dispatch_gates",
            "teatree.loop.dispatch_reducer",
        ):
            assert "teatree.loop" not in _depends_on(path), path

    def test_dispatch_reducer_is_the_lowest_dispatch_file(self) -> None:
        # `dispatch_reducer` sits just above the `dispatch_tables` leaf: it may
        # reach `scanners` + `dispatch_tables` but must NOT pull in its siblings
        # `dispatch_gates` / `dispatch` (the cluster DAG points one way).
        deps = _depends_on("teatree.loop.dispatch_reducer")
        assert "teatree.loop.dispatch_tables" in deps
        assert "teatree.loop.dispatch_gates" not in deps
        assert "teatree.loop.dispatch" not in deps

    def test_parent_loop_declares_the_dispatch_children(self) -> None:
        deps = _depends_on("teatree.loop")
        for child in (
            "teatree.loop.dispatch",
            "teatree.loop.dispatch_gates",
            "teatree.loop.dispatch_reducer",
        ):
            assert child in deps, child


_SELF_IMPROVE_ROOT = _LOOP_ROOT / "self_improve"


class TestLoopSelfImproveNode:
    """``teatree.loop.self_improve`` is a declared domain node (#2413 PR-3).

    The detector package depended only on the ``scanners`` leaf and (via the
    ``statusline`` re-export facade) on ``statusline_render.default_path``. The
    facade hop is repointed straight at the ``statusline_render`` leaf so the
    node's only intra-loop deps are two declared leaves — ``self_improve`` never
    reaches the orchestration top, and no scanner/up-edge can sneak in.
    """

    def test_self_improve_is_a_domain_node(self) -> None:
        assert _module_entry("teatree.loop.self_improve")["layer"] == "domain"

    def test_self_improve_intra_loop_deps_are_only_declared_leaves(self) -> None:
        deps = _depends_on("teatree.loop.self_improve")
        loop_deps = {d for d in deps if d.startswith("teatree.loop")}
        assert loop_deps == {"teatree.loop.scanners", "teatree.loop.statusline_render"}
        # The orchestration-top parent is NEVER a self_improve dep.
        assert "teatree.loop" not in deps

    def test_no_self_improve_module_eagerly_imports_the_statusline_facade(self) -> None:
        # `stale_statusline` reaches `default_path` via the `statusline_render`
        # leaf directly, not the `teatree.loop.statusline` facade (which carries
        # the `statusline_loops` deferred up-edges into the orchestration top).
        offenders = sorted(
            str(py.relative_to(_SELF_IMPROVE_ROOT))
            for py in _SELF_IMPROVE_ROOT.rglob("*.py")
            if "teatree.loop.statusline" in _eager_loop_imports(py)
        )
        assert offenders == [], f"self_improve eagerly importing the statusline facade: {offenders}"

    def test_parent_loop_declares_self_improve(self) -> None:
        assert "teatree.loop.self_improve" in _depends_on("teatree.loop")
