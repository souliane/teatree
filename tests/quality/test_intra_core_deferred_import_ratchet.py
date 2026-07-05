"""Shrink-ratchet for intra-``teatree.core`` deferred (function-scoped) imports (#2385).

``teatree.core`` is one tach node holding ~85 loose files plus the ``models`` /
``managers`` / ``modelkit`` sub-packages. Inside a single node tach's acyclic
guard cannot see intra-node cycles, so the FSM transition bodies, gate
predicates, and task-signal injections route their intra-``core`` edges through
**function-scoped** (deferred) imports — invisible to tach, invisible to the
human reading the module header. The #2385 sub-layering carves the lowest leaves
into their own tach nodes so the acyclic guard applies *within* ``core``; until
that carve is complete (PR-2/PR-3 sever the ``models`` / ``managers`` up-edges),
this ratchet keeps the deferred-import count from growing.

It is a per-file peg ledger (``tests/quality/deferred_import_pegs.toml``
``[intra_core]``, counted by the shared ``tests/quality/_deferred_imports.py``
walker): each source file may carry at most its pegged number of function-scoped
``teatree.core`` imports (a file not listed pegs at 0). Over-peg blocks (naming
the file); under-peg banks (lower the entry). Per-file keying makes the ledger
set-union mergeable — two disjoint peg bumps never collide, and same-file
contention surfaces as a git textual conflict, not a post-merge red (the property
a single repo-wide ``_FROZEN`` integer could not offer).

The companion ``TestCoreModelkitLayer`` pins the PR-1 carve itself: the
``teatree.core.modelkit`` leaf is declared as a ``depends_on = []`` domain node,
and the ONE top-level ``models -> gates`` edge PR-1 severs is gone — the
Fibonacci-backoff leaf was mis-filed under ``core/gates/`` and a model
(``local_stack_queue``) imported it from there; moving it into
``core/modelkit/`` cuts that edge. (The remaining ``ticket.py`` gate-predicate
edges into ``core/gates/`` are the FSM-transition bodies PR-2 severs, not PR-1.)
"""

import ast
import tomllib
from pathlib import Path

from tests.quality._deferred_imports import diff_pegs, load_pegs, per_file_counts

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_ROOT = _REPO_ROOT / "src" / "teatree" / "core"
_MODELS_ROOT = _CORE_ROOT / "models"
_TACH = _REPO_ROOT / "tach.toml"
_PREFIX = "teatree.core"
_PEG_TABLE = "intra_core"


def _module_entry(path: str) -> dict[str, object]:
    data = tomllib.loads(_TACH.read_text(encoding="utf-8"))
    return next(m for m in data["modules"] if m["path"] == path)


def _runtime_models_imports(source: Path) -> list[str]:
    """Lines importing ``teatree.core.models*`` outside an ``if TYPE_CHECKING`` block."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def under_type_checking(node: ast.AST) -> bool:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.If):
                test = cur.test
                if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                    return True
            cur = parents.get(cur)
        return False

    return [
        f"{source.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").startswith("teatree.core.models")
        and not under_type_checking(node)
    ]


def _model_files_importing(dotted: str) -> list[str]:
    hits: list[str] = []
    for py in sorted(_MODELS_ROOT.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(dotted):
                hits.append(f"{py.relative_to(_REPO_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                hits.extend(
                    f"{py.relative_to(_REPO_ROOT)}:{node.lineno}"
                    for alias in node.names
                    if alias.name.startswith(dotted)
                )
    return hits


class TestIntraCoreDeferredImportRatchet:
    def test_no_file_exceeds_its_peg(self) -> None:
        drift = diff_pegs(per_file_counts(_CORE_ROOT, _PREFIX), load_pegs(_PEG_TABLE))
        assert not drift.over_peg, (
            "intra-teatree.core deferred (function-scoped) imports grew over their per-file peg. A new "
            "function-scoped `from teatree.core... import` hides an intra-core edge from tach's acyclic "
            "guard. Make it a declared tach sub-node edge (#2385), or — if the edge is genuinely "
            "load-bearing — bump this file's peg in tests/quality/deferred_import_pegs.toml [intra_core] "
            "with a rationale in the commit message:\n" + "\n".join(drift.over_lines())
        )

    def test_no_file_is_under_its_peg(self) -> None:
        # Banks every reduction immediately: when an edge becomes a declared tach
        # sub-node edge the file's count drops, and its peg must follow it down so
        # a future regression cannot silently spend the gain.
        drift = diff_pegs(per_file_counts(_CORE_ROOT, _PREFIX), load_pegs(_PEG_TABLE))
        assert not drift.under_peg, (
            "intra-teatree.core deferred imports dropped below a per-file peg. Bank the reduction by "
            "lowering (or removing) the entry in tests/quality/deferred_import_pegs.toml [intra_core]:\n"
            + "\n".join(drift.under_lines())
        )


class TestCoreModelkitLayer:
    def test_modelkit_declared_as_depends_on_nothing_leaf(self) -> None:
        entry = _module_entry("teatree.core.modelkit")
        assert entry["layer"] == "domain"
        assert entry.get("depends_on", []) == []

    def test_parent_core_declares_modelkit_child(self) -> None:
        parent = _module_entry("teatree.core")
        assert "teatree.core.modelkit" in parent.get("depends_on", [])

    def test_models_no_longer_import_the_fibonacci_gate_leaf(self) -> None:
        # The single top-level models -> gates edge PR-1 severs: the Fibonacci
        # backoff leaf was mis-filed at teatree.core.gates.fibonacci and the
        # local_stack_queue model imported it from there. It now lives under
        # teatree.core.modelkit; a model reaching back to the gates path
        # resurrects the severed edge.
        hits = _model_files_importing("teatree.core.gates.fibonacci")
        assert not hits, (
            "a model imports the moved Fibonacci leaf from teatree.core.gates "
            "(the severed #2385 PR-1 edge):\n" + "\n".join(hits)
        )

    def test_fibonacci_leaf_lives_under_modelkit_not_gates(self) -> None:
        assert (_CORE_ROOT / "modelkit" / "fibonacci.py").is_file()
        assert not (_CORE_ROOT / "gates" / "fibonacci.py").exists()


class TestCoreModelManagerNodes:
    """Pins the PR-2b carve of the ``models`` / ``managers`` tach sub-nodes.

    ``teatree.core.models`` and ``teatree.core.managers`` are declared tach
    sub-nodes, so the core models↔managers cycle is structurally forbidden by
    ``forbid_circular_dependencies`` rather than merely severed.

    ``models`` depends on ``managers`` (the eager ``objects = XManager()`` edge),
    one-way: ``managers`` may never depend on ``models`` (the pure exception leaf
    ``models.errors`` is its own node so a manager-imported exception edge does
    not pull ``managers`` into the full ``models`` node). The two manager-layer
    helper leaves (``loop_lease_manager`` / ``session_handover_manager``, split out
    of ``managers``) are declared below ``managers`` so it never reaches back up
    into the parent ``teatree.core`` node.
    """

    def test_models_declared_depending_on_managers_one_way(self) -> None:
        entry = _module_entry("teatree.core.models")
        deps = entry.get("depends_on", [])
        assert entry["layer"] == "domain"
        assert "teatree.core.managers" in deps

    def test_managers_never_depends_on_the_models_node(self) -> None:
        entry = _module_entry("teatree.core.managers")
        deps = entry.get("depends_on", [])
        assert entry["layer"] == "domain"
        assert "teatree.core.models" not in deps
        assert "teatree.core" not in deps

    def test_models_errors_is_a_pure_leaf_node(self) -> None:
        entry = _module_entry("teatree.core.models.errors")
        assert entry["layer"] == "domain"
        assert entry.get("depends_on", []) == []

    def test_parent_core_declares_models_and_errors_children(self) -> None:
        parent = _module_entry("teatree.core")
        deps = parent.get("depends_on", [])
        assert "teatree.core.models" in deps
        assert "teatree.core.models.errors" in deps

    def test_managers_has_no_runtime_models_import(self) -> None:
        # The 17 deferred `from teatree.core.models.X import Y` imports inside
        # manager methods became `apps.get_model("core", "Y")`. A runtime
        # `from teatree.core.models...` import in managers.py (top-level OR
        # function-scoped, but not TYPE_CHECKING-guarded) resurrects the forbidden
        # managers→models edge.
        offenders = _runtime_models_imports(_CORE_ROOT / "managers.py")
        assert not offenders, (
            "managers.py imports the teatree.core.models node at runtime "
            "(use apps.get_model instead):\n" + "\n".join(offenders)
        )
