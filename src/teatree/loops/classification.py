"""The guard behind the loop tags: no loop ships unclassified, and no ``deterministic`` lies.

:func:`unclassified_loops` is the shipping gate — a ``MINI_LOOP`` declaring no
reach set or no determinism is named here and fails
``tests/conformance/test_loop_classification.py``.

:func:`ai_evidence` is the cross-check on the half of the classification a
hand-written label can get catastrophically wrong. ``deterministic`` is a promise
that a tick costs nothing and cannot surprise the owner, so it is verified against
the loop's real dispatch behaviour rather than trusted. Three routes to a model
are read out of the source the caller supplies:

* a signal kind that dispatches an agent — see :func:`agent_dispatching_kinds`;
* a ``phase=`` argument naming an agent-dispatched phase — see :func:`agent_dispatched_phases`;
* an import of a model-calling module — see :data:`MODEL_CALLING_MODULES`.

A signal routed to a MECHANICAL executor is followed too: the executor is resolved
through :data:`teatree.loop.mechanical.HANDLERS` and its import closure scanned for
those same model-calling modules, because a handler that reads as mechanical in the
dispatch table can still spawn a headless turn several modules down. The closure
stops at the aggregator modules that re-export the whole dispatch surface —
following those would pull every handler into every loop.

The evidence is one-directional by construction: finding a route proves the loop
reaches a model, finding none proves nothing. A scanner built only behind an
opt-in setting contributes no source to read, so a genuinely ``ai`` loop can go
unconfirmed — which is why the conformance lane asserts ``derived ⊆ declared-ai``
and never the reverse.
"""

import ast
import importlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import teatree.loops as _loops_pkg
from teatree.core.modelkit import phases
from teatree.loop.dispatch import conditional_dispatch_kinds
from teatree.loop.dispatch_tables import AGENT_BY_KIND, MECHANICAL_BY_KIND
from teatree.loop.mechanical import HANDLERS
from teatree.loops.base import MiniLoop

#: Importing any of these means the module can run a model turn. Named exactly
#: rather than by ``teatree.agents`` prefix, so a type-only import of an agent
#: RESULT schema is not mistaken for a call.
MODEL_CALLING_MODULES: tuple[str, ...] = (
    "claude_agent_sdk",
    "teatree.agents.headless",
    "teatree.agents.one_shot",
    "teatree.agents.pydantic_ai_session",
)

#: Keyword arguments whose value names the phase a dispatched ``Task`` runs under.
#: Phase tokens are matched HERE rather than as bare string constants: half the
#: vocabulary is ordinary English (``coding``, ``testing``, ``e2e``), so a loose
#: match would turn a deterministic loop red over a log line.
_PHASE_KEYWORDS: frozenset[str] = frozenset({"phase", "target_phase"})

#: Modules the mechanical closure refuses to follow — each re-exports the whole
#: dispatch surface, so following one puts every handler in every loop's closure.
_CLOSURE_AGGREGATORS: frozenset[str] = frozenset(
    {
        "teatree.loop.dispatch",
        "teatree.loop.dispatch_gates",
        "teatree.loop.dispatch_reducer",
        "teatree.loop.dispatch_tables",
        "teatree.loop.domain_jobs",
        "teatree.loop.mechanical",
        "teatree.loop.persistence",
        "teatree.loop.scanners",
    }
)

_CLOSURE_ROOTS: tuple[str, ...] = ("teatree.loop.", "teatree.loops.")


@dataclass(frozen=True, slots=True)
class _SourceEvidence:
    """What one pass over a module tree found: proven routes, plus leads to follow."""

    routes: frozenset[str]
    mechanical_kinds: frozenset[str]


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


def mechanical_executor_kinds() -> frozenset[str]:
    """Signal kinds the dispatch table routes to a mechanical executor rather than an agent."""
    return frozenset(kind for kind, (action, _zone) in MECHANICAL_BY_KIND.items() if action == "mechanical")


def loop_package_sources(name: str) -> tuple[Path, ...]:
    """Every Python file in one mini-loop's own package."""
    return tuple(sorted((Path(str(_loops_pkg.__file__)).parent / name).rglob("*.py")))


def ai_evidence(sources: Iterable[Path]) -> tuple[str, ...]:
    """Every route to a model reachable from *sources*, including via mechanical executors."""
    found = _scan(sources)
    return tuple(sorted(found.routes | _mechanical_closure_routes(found.mechanical_kinds)))


def _scan(sources: Iterable[Path]) -> _SourceEvidence:
    kinds = agent_dispatching_kinds()
    dispatched_phases = agent_dispatched_phases()
    mechanical = mechanical_executor_kinds()
    routes: set[str] = set()
    leads: set[str] = set()
    for path in set(sources):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = _code_strings(tree)
        routes |= literals & kinds
        routes |= (_phase_arguments(tree) | _resolved_phase_imports(tree)) & dispatched_phases
        routes |= _model_calling_imports(tree)
        leads |= literals & mechanical
    return _SourceEvidence(routes=frozenset(routes), mechanical_kinds=frozenset(leads))


def _mechanical_closure_routes(mechanical_kinds: Iterable[str]) -> frozenset[str]:
    """Model-calling imports reachable from the executors *mechanical_kinds* route to."""
    zones = (MECHANICAL_BY_KIND[kind][1] for kind in mechanical_kinds)
    entry_points = {HANDLERS[zone].__module__ for zone in zones if zone in HANDLERS}
    routes: set[str] = set()
    for module in _import_closure(entry_points):
        routes |= _model_calling_imports(_parse_module(module))
    return frozenset(routes)


def _import_closure(entry_points: Iterable[str]) -> frozenset[str]:
    seen: set[str] = set()
    pending = [name for name in entry_points if _is_followable(name)]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        reachable = _imported_modules(_parse_module(module))
        pending.extend(name for name in reachable if _is_followable(name) and name not in seen)
    return frozenset(seen)


def _parse_module(name: str) -> ast.Module:
    return ast.parse(Path(str(importlib.import_module(name).__file__)).read_text(encoding="utf-8"))


def _is_followable(module: str) -> bool:
    return module.startswith(_CLOSURE_ROOTS) and module not in _CLOSURE_AGGREGATORS


def _imported_modules(tree: ast.Module) -> frozenset[str]:
    from_imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    plain = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    return frozenset(from_imports | plain)


def _model_calling_imports(tree: ast.Module) -> frozenset[str]:
    type_only = _type_only_imports(tree)
    runtime = [
        node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in type_only
    ]
    named = {node.module or "" for node in runtime if isinstance(node, ast.ImportFrom)}
    named |= {alias.name for node in runtime if isinstance(node, ast.Import) for alias in node.names}
    return frozenset(module for module in named if _is_model_calling(module))


def _type_only_imports(tree: ast.Module) -> list[ast.stmt]:
    """Imports under ``if TYPE_CHECKING:`` — annotations only, never a runtime call."""
    guards = (node for node in ast.walk(tree) if isinstance(node, ast.If) and _is_type_checking_guard(node.test))
    return [node for guard in guards for node in ast.walk(guard) if isinstance(node, (ast.Import, ast.ImportFrom))]


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _is_model_calling(module: str) -> bool:
    return any(module == named or module.startswith(f"{named}.") for named in MODEL_CALLING_MODULES)


def _code_strings(tree: ast.Module) -> frozenset[str]:
    return frozenset(
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _phase_arguments(tree: ast.Module) -> frozenset[str]:
    keywords = [node for node in ast.walk(tree) if isinstance(node, ast.keyword) and node.arg in _PHASE_KEYWORDS]
    values = (node.value for node in keywords)
    return frozenset(node.value for node in values if isinstance(node, ast.Constant) and isinstance(node.value, str))


def _resolved_phase_imports(tree: ast.Module) -> frozenset[str]:
    """Phase tokens imported by NAME — a scanner re-exports the constant, not the literal."""
    aliases = (
        alias
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == phases.__name__
        for alias in node.names
    )
    resolved = (getattr(phases, alias.name, None) for alias in aliases)
    return frozenset(value for value in resolved if isinstance(value, str))
