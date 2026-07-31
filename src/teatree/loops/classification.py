"""The guard behind the loop tags: no loop ships unclassified, and no ``deterministic`` lies.

:func:`unclassified_loops` is the shipping gate — a ``MINI_LOOP`` declaring no
reach set or no determinism is named here and fails
``tests/conformance/test_loop_classification.py``.

:func:`ai_evidence` is the cross-check on the half of the classification a
hand-written label can get catastrophically wrong. ``deterministic`` is a promise
that a tick costs nothing and cannot surprise the owner, so it is verified against
the loop's real dispatch behaviour rather than trusted: :func:`agent_dispatch_vocabulary`
reads the live routing tables for every token that puts an agent on the other end
— the ``AGENT_BY_KIND`` signal kinds, the ``("agent", …)`` rows of
``MECHANICAL_BY_KIND``, the payload-conditional handlers, and every phase in
``SUBAGENT_BY_PHASE`` / ``SCANNER_DISPATCHED_PHASES`` — and the extractor looks for
those tokens, plus any import of a model-calling module, in the source the caller
supplies. A new agent route widens the vocabulary automatically.

The evidence is one-directional by construction: finding a token proves the loop
reaches an agent, finding none proves nothing. A scanner built only behind an
opt-in setting contributes no source to read, so a genuinely ``ai`` loop can go
unconfirmed — which is why the conformance lane asserts ``derived ⊆ declared-ai``
and never the reverse.
"""

import ast
from collections.abc import Iterable
from pathlib import Path

import teatree.loops as _loops_pkg
from teatree.core.modelkit import phases
from teatree.loop.dispatch import conditional_dispatch_kinds
from teatree.loop.dispatch_tables import AGENT_BY_KIND, MECHANICAL_BY_KIND
from teatree.loops.base import MiniLoop

#: Import roots that mean "this module can call a model". Matched on the dotted
#: prefix, so a submodule of either counts.
MODEL_CALLING_ROOTS: tuple[str, ...] = ("claude_agent_sdk", "teatree.agents")


def unclassified_loops(loops: Iterable[MiniLoop]) -> tuple[str, ...]:
    """Every loop that declares no reach set or no determinism, in registry order."""
    return tuple(loop.name for loop in loops if loop.declared_reach is None or loop.determinism is None)


def agent_dispatching_kinds() -> frozenset[str]:
    """Every ``ScanSignal.kind`` whose dispatch can put an agent on the other end."""
    mechanical = frozenset(kind for kind, (action, _zone) in MECHANICAL_BY_KIND.items() if action == "agent")
    return frozenset(AGENT_BY_KIND) | mechanical | conditional_dispatch_kinds()


def agent_dispatched_phases() -> frozenset[str]:
    """Every ``Task`` phase that resolves to an agent — routed or free-form headless."""
    return frozenset(phase for _role, phase in phases.SUBAGENT_BY_PHASE) | phases.SCANNER_DISPATCHED_PHASES


def agent_dispatch_vocabulary() -> frozenset[str]:
    return agent_dispatching_kinds() | agent_dispatched_phases()


def loop_package_sources(name: str) -> tuple[Path, ...]:
    """Every Python file in one mini-loop's own package."""
    return tuple(sorted((Path(str(_loops_pkg.__file__)).parent / name).rglob("*.py")))


def ai_evidence(sources: Iterable[Path]) -> tuple[str, ...]:
    """Every agent-dispatch token and model-calling import reachable in *sources*.

    Docstrings are excluded so a module that merely *documents* a route it does not
    take stays clean — the prose in this tree names dispatch kinds constantly.
    Phase tokens imported by name from the canonical phase vocabulary are resolved
    to their values, because a scanner re-exports ``ARCHITECTURAL_REVIEW_PHASE``
    rather than writing the literal.
    """
    vocabulary = agent_dispatch_vocabulary()
    found: set[str] = set()
    for path in set(sources):
        found |= _evidence_in(ast.parse(path.read_text(encoding="utf-8")), vocabulary)
    return tuple(sorted(found))


def _evidence_in(tree: ast.Module, vocabulary: frozenset[str]) -> set[str]:
    prose = _docstring_ids(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in vocabulary and id(node) not in prose:
            found.add(str(node.value))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found |= _imported_from_evidence(node, vocabulary)
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if _is_model_calling(alias.name)}
    return found


def _imported_from_evidence(node: ast.ImportFrom, vocabulary: frozenset[str]) -> set[str]:
    module = node.module or ""
    if _is_model_calling(module):
        return {module}
    if module != phases.__name__:
        return set()
    resolved = (getattr(phases, alias.name, None) for alias in node.names)
    return {value for value in resolved if isinstance(value, str) and value in vocabulary}


def _is_model_calling(module: str) -> bool:
    return any(module == root or module.startswith(f"{root}.") for root in MODEL_CALLING_ROOTS)


def _docstring_ids(tree: ast.Module) -> frozenset[int]:
    """The node ids of every docstring, so prose is not mistaken for a dispatch."""
    scopes = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    documented = (node for node in ast.walk(tree) if isinstance(node, scopes) and node.body)
    leading = (node.body[0] for node in documented)
    return frozenset(
        id(statement.value)
        for statement in leading
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
    )
