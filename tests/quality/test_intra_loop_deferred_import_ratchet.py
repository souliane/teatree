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

It is a per-file peg ledger (``tests/quality/deferred_import_pegs.toml``
``[intra_loop]``, counted by the shared ``tests/quality/_deferred_imports.py``
walker): each source file may carry at most its pegged number of function-scoped
``teatree.loop`` imports (a file not listed pegs at 0). Over-peg blocks (naming
the file); under-peg banks (lower the entry). Per-file keying makes the ledger
set-union mergeable — two disjoint peg bumps never collide, and same-file
contention surfaces as a git textual conflict, not a post-merge red.

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

from tests.quality._deferred_imports import diff_pegs, load_pegs, per_file_counts

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOOP_ROOT = _REPO_ROOT / "src" / "teatree" / "loop"
_TACH = _REPO_ROOT / "tach.toml"
_PREFIX = "teatree.loop"
_PEG_TABLE = "intra_loop"


def _module_entry(path: str) -> dict[str, object]:
    data = tomllib.loads(_TACH.read_text(encoding="utf-8"))
    return next(m for m in data["modules"] if m["path"] == path)


def _depends_on(path: str) -> list[str]:
    raw = _module_entry(path).get("depends_on", [])
    assert isinstance(raw, list)
    return [str(dep) for dep in raw]


class TestIntraLoopDeferredImportRatchet:
    def test_no_file_exceeds_its_peg(self) -> None:
        drift = diff_pegs(per_file_counts(_LOOP_ROOT, _PREFIX), load_pegs(_PEG_TABLE))
        assert not drift.over_peg, (
            "intra-teatree.loop deferred (function-scoped) imports grew over their per-file peg. A new "
            "function-scoped `from teatree.loop... import` hides an intra-loop edge from tach's acyclic "
            "guard. Make it a declared tach sub-node edge (#2413), or — if the edge is genuinely "
            "load-bearing — bump this file's peg in tests/quality/deferred_import_pegs.toml [intra_loop] "
            "with a rationale in the commit message:\n" + "\n".join(drift.over_lines())
        )

    def test_no_file_is_under_its_peg(self) -> None:
        # Banks every reduction immediately: when an edge becomes a declared tach
        # sub-node edge the file's count drops, and its peg must follow it down so
        # a future regression cannot silently spend the gain.
        drift = diff_pegs(per_file_counts(_LOOP_ROOT, _PREFIX), load_pegs(_PEG_TABLE))
        assert not drift.under_peg, (
            "intra-teatree.loop deferred imports dropped below a per-file peg. Bank the reduction by "
            "lowering (or removing) the entry in tests/quality/deferred_import_pegs.toml [intra_loop]:\n"
            + "\n".join(drift.under_lines())
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
        assert set(entry.get("depends_on", [])) == {
            "teatree.types",
            "teatree.core.models",
            "teatree.loop.loop_state_db",
        }

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


class TestLoopStatuslineLoopsNode:
    """The ``statusline_loops`` back-edge is severed and declared (#2413 PR-4).

    ``statusline_loops`` (the dedicated loop-line dashboard, a low presentation
    module) used to defer-import UP into the orchestration top — the four cadence
    readers on ``tick_piggyback`` / ``queue_drain`` and the ``loop_scoping`` slot
    filter — so ``statusline`` could not be a declared node (any eager edge into
    it would surface the cycle). PR-4 extracts the four pure ``os.environ``
    cadence readers into the ``teatree.loop.loop_cadences`` leaf; ``statusline_loops``
    now reaches them, ``loop_scoping``, and the ``statusline_palette`` leaf via
    eager DOWN edges, so the whole ``statusline`` facade is declarable and the
    back-edge is structurally forbidden, not merely deferred out of sight.
    """

    def test_loop_cadences_is_a_pure_leaf_node(self) -> None:
        entry = _module_entry("teatree.loop.loop_cadences")
        assert entry["layer"] == "domain"
        assert entry.get("depends_on", []) == []

    def test_session_identity_is_an_intra_loop_leaf_node(self) -> None:
        # The loop-side re-export of the session identity primitive — `loop_scoping`
        # reaches it for `current_session_id`. It only re-exports from
        # `teatree.core.session_identity`, so it has NO intra-loop dependency.
        entry = _module_entry("teatree.loop.session_identity")
        assert entry["layer"] == "domain"
        loop_deps = {d for d in _depends_on("teatree.loop.session_identity") if d.startswith("teatree.loop")}
        assert loop_deps == set()

    def test_loop_scoping_is_a_leaf_over_session_identity(self) -> None:
        entry = _module_entry("teatree.loop.loop_scoping")
        assert entry["layer"] == "domain"
        deps = set(_depends_on("teatree.loop.loop_scoping"))
        assert "teatree.loop.session_identity" in deps
        # `loop_scoping` never reaches the orchestration-top parent.
        assert "teatree.loop" not in deps

    def test_statusline_loops_depends_only_on_declared_leaves(self) -> None:
        entry = _module_entry("teatree.loop.statusline_loops")
        assert entry["layer"] == "domain"
        loop_deps = {d for d in _depends_on("teatree.loop.statusline_loops") if d.startswith("teatree.loop")}
        assert loop_deps == {
            "teatree.loop.statusline_palette",
            "teatree.loop.loop_cadences",
            "teatree.loop.loop_scoping",
        }
        # The severed back-edges: `statusline_loops` NEVER depends on the
        # orchestration-top cadence homes nor the parent node.
        assert "teatree.loop.tick_piggyback" not in loop_deps
        assert "teatree.loop.queue_drain" not in loop_deps
        assert "teatree.loop" not in loop_deps

    def test_statusline_facade_depends_on_the_two_render_leaves(self) -> None:
        entry = _module_entry("teatree.loop.statusline")
        assert entry["layer"] == "domain"
        deps = set(_depends_on("teatree.loop.statusline"))
        assert deps == {"teatree.loop.statusline_loops", "teatree.loop.statusline_render"}

    def test_parent_loop_declares_the_new_children(self) -> None:
        deps = _depends_on("teatree.loop")
        for child in (
            "teatree.loop.loop_cadences",
            "teatree.loop.session_identity",
            "teatree.loop.loop_scoping",
            "teatree.loop.statusline_loops",
            "teatree.loop.statusline",
        ):
            assert child in deps, child

    def test_statusline_loops_does_not_defer_import_the_orchestration_top(self) -> None:
        # The four cadence readers + the loop_scoping filter are now eager DOWN
        # edges; no `statusline_loops` line may function-scope-import the
        # `tick_piggyback` / `queue_drain` cadence homes (the severed cycle).
        source = (_LOOP_ROOT / "statusline_loops.py").read_text(encoding="utf-8")
        assert "teatree.loop.tick_piggyback" not in source
        assert "teatree.loop.queue_drain" not in source


class TestLoopRenderingNode:
    """The ``rendering`` flat-file cluster is declared as tach domain nodes (#2413 PR-4 slice).

    The cluster (``rendering`` facade over ``rendering_classification`` /
    ``rendering_dms`` / ``rendering_items`` / ``rendering_permalinks`` /
    ``rendering_zones``) was eager-clean — every intra-loop edge already pointed
    DOWN to already-declared nodes (``dispatch`` / ``statusline`` /
    ``statusline_render`` / ``pr_ticket_index``) or to a sibling rendering file —
    but lived inside the single ``teatree.loop`` node, so tach's acyclic guard
    could not see it. Declaring each flat file as its own ``domain`` node makes
    the within-cluster DAG enforced: a future back-edge (a rendering file
    importing the orchestration top, or ``rendering_items`` importing the facade)
    is now a tach failure, not an invisible cycle. The one redundant deferred
    edge in ``rendering.py`` (``live_loops_anchor`` from ``statusline``, already
    imported eagerly on the same module) is hoisted to the eager import, banking
    the ratchet one step.
    """

    def test_rendering_cluster_files_are_domain_nodes(self) -> None:
        for path in (
            "teatree.loop.rendering",
            "teatree.loop.rendering_classification",
            "teatree.loop.rendering_dms",
            "teatree.loop.rendering_items",
            "teatree.loop.rendering_permalinks",
            "teatree.loop.rendering_zones",
        ):
            assert _module_entry(path)["layer"] == "domain", path

    def test_rendering_items_is_a_pure_intra_loop_leaf(self) -> None:
        # `rendering_items` has no intra-loop dependency — it is the bottom of
        # the rendering DAG (its only non-loop dep is `teatree.url_classify`).
        loop_deps = {d for d in _depends_on("teatree.loop.rendering_items") if d.startswith("teatree.loop")}
        assert loop_deps == set()

    def test_no_rendering_file_depends_on_the_orchestration_top(self) -> None:
        # No file in the rendering cluster may declare a dependency on the
        # orchestration-top `teatree.loop` (the parent) — that is the back-edge
        # the carve forbids structurally. Rendering is a pure DOWN leaf consumed
        # only by `phases.render` / `tick_freshness` in the orchestration top.
        for path in (
            "teatree.loop.rendering",
            "teatree.loop.rendering_classification",
            "teatree.loop.rendering_dms",
            "teatree.loop.rendering_items",
            "teatree.loop.rendering_permalinks",
            "teatree.loop.rendering_zones",
        ):
            assert "teatree.loop" not in _depends_on(path), path

    def test_rendering_facade_reaches_only_declared_leaves(self) -> None:
        # The facade depends on its sibling rendering files plus the already-
        # declared `dispatch` / `statusline` / `pr_ticket_index` leaves — never
        # the orchestration top.
        loop_deps = {d for d in _depends_on("teatree.loop.rendering") if d.startswith("teatree.loop")}
        assert loop_deps == {
            "teatree.loop.dispatch",
            "teatree.loop.pr_ticket_index",
            "teatree.loop.statusline",
            "teatree.loop.rendering_classification",
            "teatree.loop.rendering_items",
            "teatree.loop.rendering_permalinks",
            "teatree.loop.rendering_zones",
        }

    def test_rendering_does_not_defer_import_the_statusline_facade(self) -> None:
        # `live_loops_anchor` is reached via the eager `statusline` import on the
        # module header; no function-scoped `from teatree.loop.statusline import`
        # remains in `rendering.py` (the redundant deferral PR-4 hoists).
        source = (_LOOP_ROOT / "rendering.py").read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in source.splitlines()
            if line.lstrip().startswith("from teatree.loop.statusline import") and line != line.lstrip()
        ]
        assert offenders == [], f"rendering.py still defers a statusline import: {offenders}"

    def test_parent_loop_declares_the_rendering_children(self) -> None:
        deps = _depends_on("teatree.loop")
        for child in (
            "teatree.loop.rendering",
            "teatree.loop.rendering_classification",
            "teatree.loop.rendering_dms",
            "teatree.loop.rendering_items",
            "teatree.loop.rendering_permalinks",
            "teatree.loop.rendering_zones",
        ):
            assert child in deps, child


_SLACK_ANSWER_ROOT = _LOOP_ROOT / "slack_answer"


class TestLoopSlackAnswerNode:
    """``teatree.loop.slack_answer`` is a declared domain node (#2413 slice).

    The reactive Slack-answer subpackage (``classifier`` / ``simple_answer`` /
    ``thread_readback`` / ``cycle``) was already eager-clean — ZERO intra-loop
    deferred imports inside the subpackage, every intra-loop edge pointing DOWN
    to already-declared nodes (``self_improve`` budget seam, ``statusline``) or a
    sibling slack_answer file — but lived inside the single ``teatree.loop``
    node, so tach's acyclic guard could not see it. Declaring the subpackage as
    its own ``domain`` node (mirroring the ``scanners`` / ``self_improve``
    sibling-package nodes) makes a future back-edge — a slack_answer file
    importing the orchestration top — a tach failure instead of an invisible
    cycle. The one deferred up-edge that targeted slack_answer lived in the
    orchestration-top ``tick_piggyback._piggyback_slack_answer`` (a deferred
    ``from teatree.loop.slack_answer.cycle import run_slack_answer_cycle``); it
    is hoisted to the module header — an eager declared parent->child edge —
    banking the ratchet one step.
    """

    def test_slack_answer_is_a_domain_node(self) -> None:
        assert _module_entry("teatree.loop.slack_answer")["layer"] == "domain"

    def test_slack_answer_intra_loop_deps_are_only_declared_leaves(self) -> None:
        deps = _depends_on("teatree.loop.slack_answer")
        loop_deps = {d for d in deps if d.startswith("teatree.loop")}
        assert loop_deps == {"teatree.loop.self_improve", "teatree.loop.statusline"}
        # The orchestration-top parent is NEVER a slack_answer dep.
        assert "teatree.loop" not in deps

    def test_no_slack_answer_file_eagerly_imports_the_orchestration_top(self) -> None:
        # No file in the subpackage may eager-import a flat orchestration-top
        # module (`tick*` / `queue_drain` / `mechanical` / `phases`) — that is
        # the back-edge the carve forbids structurally.
        offenders: list[str] = []
        for py in _SLACK_ANSWER_ROOT.rglob("*.py"):
            for mod in _eager_loop_imports(py):
                tail = mod[len("teatree.loop.") :]
                if tail.startswith(("tick", "queue_drain", "mechanical", "phases")):
                    offenders.append(f"{py.relative_to(_SLACK_ANSWER_ROOT)} -> {mod}")
        assert offenders == [], f"slack_answer eagerly importing the orchestration top: {sorted(offenders)}"

    def test_parent_loop_declares_slack_answer(self) -> None:
        assert "teatree.loop.slack_answer" in _depends_on("teatree.loop")
