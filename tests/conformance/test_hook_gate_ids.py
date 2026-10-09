"""Every hook deny names its gate with a declared id that no other gate uses."""

import ast
from collections import defaultdict
from pathlib import Path

import pytest

from hooks.scripts.gate_ledger import DECLARED_GATES

_SCRIPTS = Path(__file__).resolve().parents[2] / "hooks" / "scripts"
_DENY_ROUTES = frozenset({"emit_pretooluse_deny", "_fail_open_or_deny"})
_DENY_WRITERS = frozenset({"write_pretooluse_deny", "_write_pretooluse_deny"})
_ROUTER = "hook_router.py"
_ROUTER_ROUTES = frozenset((_ROUTER, name) for name in _DENY_ROUTES)
_WRITER_CALLER = (_ROUTER, "emit_pretooluse_deny")
_ONE_GATE_SPLIT_OVER_HELPERS = {
    "banned_terms": frozenset(
        {
            ("banned_terms/deny.py", "emit_banned_term_deny"),
            ("banned_terms/gate.py", "_banned_term_marker_blocks"),
            ("banned_terms/gate.py", "_run_banned_terms_pretool"),
        }
    ),
    "mr_metadata": frozenset(
        {("hook_router.py", "_handle_broken_validate_env"), ("hook_router.py", "_mr_validator_verdict")}
    ),
}


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


class _DenySites(ast.NodeVisitor):
    def __init__(self, module: str, tree: ast.Module) -> None:
        self.module = module
        self.aliases = {
            alias.asname: alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.asname and alias.name in _DENY_ROUTES | _DENY_WRITERS
        }
        self.functions: list[str] = []
        self.called: set[int] = set()
        self.sites: list[tuple[str | None, str, int]] = []
        self.problems: list[str] = []

    def _enclosing(self) -> str:
        return self.functions[-1] if self.functions else "<module>"

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.asname and alias.name in _DENY_ROUTES | _DENY_WRITERS and self.module != _ROUTER:
                self.problems.append(
                    f"{self.module}:{node.lineno} {self._enclosing()}: "
                    f"imports deny route {alias.name!r} as {alias.asname!r}"
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self.called.add(id(node.func))
        route = self.aliases.get(_callee(node.func), _callee(node.func))
        enclosing = self._enclosing()
        looked_up = node.args[1] if route == "getattr" and len(node.args) > 1 else None
        if isinstance(looked_up, ast.Constant) and looked_up.value in _DENY_ROUTES | _DENY_WRITERS:
            self.problems.append(
                f"{self.module}:{node.lineno} {enclosing}: reaches deny route {looked_up.value!r} through getattr"
            )
        elif route in _DENY_WRITERS and (self.module, enclosing) != _WRITER_CALLER:
            self.problems.append(
                f"{self.module}:{node.lineno} {enclosing}: writes a deny outside hook_router.emit_pretooluse_deny"
            )
        elif route in _DENY_ROUTES and (self.module, enclosing) not in _ROUTER_ROUTES:
            gate = next((keyword.value for keyword in node.keywords if keyword.arg == "gate_id"), None)
            value = gate.value if isinstance(gate, ast.Constant) and isinstance(gate.value, str) else None
            self.sites.append((value, enclosing, node.lineno))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._reference(node, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._reference(node, node.attr)
        self.generic_visit(node)

    def _reference(self, node: ast.Name | ast.Attribute, name: str) -> None:
        route = self.aliases.get(name, name)
        if id(node) not in self.called and isinstance(node.ctx, ast.Load) and route in _DENY_ROUTES | _DENY_WRITERS:
            self.problems.append(
                f"{self.module}:{node.lineno} {self._enclosing()}: deny route {route!r} used without a call"
            )


def hook_sources() -> dict[str, str]:
    return {
        str(path.relative_to(_SCRIPTS)): path.read_text(encoding="utf-8") for path in sorted(_SCRIPTS.rglob("*.py"))
    }


def _scan(module: str, source: str) -> _DenySites:
    tree = ast.parse(source)
    visitor = _DenySites(module, tree)
    visitor.visit(tree)
    return visitor


def deny_site_functions(sources: dict[str, str]) -> set[tuple[str, str]]:
    return {
        (module, function)
        for module, source in sources.items()
        for _gate, function, _line in _scan(module, source).sites
    }


def gate_id_problems(sources: dict[str, str], declared: frozenset[str]) -> tuple[list[str], int]:
    owners: dict[str, set[tuple[str, str]]] = defaultdict(set)
    problems: list[str] = []
    count = 0
    for module, source in sources.items():
        visitor = _scan(module, source)
        problems.extend(visitor.problems)
        for gate, function, line in visitor.sites:
            count += 1
            if gate is None:
                problems.append(f"{module}:{line} {function}: deny without a constant gate_id")
            elif gate not in declared:
                problems.append(f"{module}:{line} {function}: gate_id {gate!r} is not declared in the ledger")
            else:
                owners[gate].add((module, function))
    for gate, sites in owners.items():
        if len(sites) > 1 and not sites <= _ONE_GATE_SPLIT_OVER_HELPERS.get(gate, frozenset()):
            problems.append(f"gate_id {gate!r} is stamped by several gates: {sorted(sites)}")
    problems.extend(f"declared gate_id {gate!r} is stamped by no deny" for gate in sorted(declared - owners.keys()))
    return problems, count


def test_every_hook_deny_stamps_a_declared_gate_id_owned_by_one_gate() -> None:
    problems, count = gate_id_problems(hook_sources(), DECLARED_GATES)

    assert count >= len(DECLARED_GATES)
    assert problems == []


def test_the_check_refuses_a_missing_an_undeclared_and_a_reused_gate_id() -> None:
    source = """
def handle_a(data):
    return _fail_open_or_deny(data, "BLOCKED: one", gate_id="shared")

def handle_b(data):
    return emit_pretooluse_deny("BLOCKED: two", gate_id="shared")

def handle_nameless(data):
    return emit_pretooluse_deny("BLOCKED: three")

def handle_unknown(data):
    return _fail_open_or_deny(data, "BLOCKED: four", gate_id="nobody_declared_me")
"""
    problems, count = gate_id_problems({"synthetic.py": source}, frozenset({"shared", "unused"}))

    assert count == 4
    assert problems == [
        "synthetic.py:9 handle_nameless: deny without a constant gate_id",
        "synthetic.py:12 handle_unknown: gate_id 'nobody_declared_me' is not declared in the ledger",
        "gate_id 'shared' is stamped by several gates: [('synthetic.py', 'handle_a'), ('synthetic.py', 'handle_b')]",
        "declared gate_id 'unused' is stamped by no deny",
    ]


_ALIASED_ROUTE = """
from hooks.scripts.hook_router import emit_pretooluse_deny as _deny

def handle_aliased(data):
    return _deny("BLOCKED: aliased")
"""
_BOUND_ROUTE = """
import functools
from hooks.scripts.hook_router import emit_pretooluse_deny

_deny = functools.partial(emit_pretooluse_deny, gate_id=None)

def handle_bound(data):
    return _deny("BLOCKED: bound")
"""
_DIRECT_WRITER = """
from hooks.scripts.gate_decision import write_pretooluse_deny

def handle_direct(data):
    return write_pretooluse_deny("BLOCKED: direct", gate_id="shared", context=("PreToolUse", {}))
"""
_ASYNC_DENY = """
async def handle_async(data):
    return _fail_open_or_deny(data, "BLOCKED: async")
"""


_GETATTR_ROUTE = """
import hooks.scripts.hook_router as hr

def handle_getattr(data):
    return getattr(hr, "emit_pretooluse_deny")("BLOCKED: getattr")
"""
_RENAMED_REEXPORT = """
from hooks.scripts.hook_router import emit_pretooluse_deny as refuse
"""
_LOCAL_EMITTER = """
def emit_pretooluse_deny(reason, gate_id=None):
    return write_pretooluse_deny(reason, gate_id=None, context=("PreToolUse", {}))
"""
_LOCAL_FAIL_OPEN = """
def _fail_open_or_deny(data, reason, gate_id=None):
    return emit_pretooluse_deny(reason)
"""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            _ALIASED_ROUTE,
            [
                "synthetic.py:2 <module>: imports deny route 'emit_pretooluse_deny' as '_deny'",
                "synthetic.py:5 handle_aliased: deny without a constant gate_id",
            ],
        ),
        (_BOUND_ROUTE, ["synthetic.py:5 <module>: deny route 'emit_pretooluse_deny' used without a call"]),
        (_DIRECT_WRITER, ["synthetic.py:5 handle_direct: writes a deny outside hook_router.emit_pretooluse_deny"]),
        (_ASYNC_DENY, ["synthetic.py:3 handle_async: deny without a constant gate_id"]),
        (_GETATTR_ROUTE, ["synthetic.py:5 handle_getattr: reaches deny route 'emit_pretooluse_deny' through getattr"]),
        (_RENAMED_REEXPORT, ["synthetic.py:2 <module>: imports deny route 'emit_pretooluse_deny' as 'refuse'"]),
        (
            _LOCAL_EMITTER,
            ["synthetic.py:3 emit_pretooluse_deny: writes a deny outside hook_router.emit_pretooluse_deny"],
        ),
        (_LOCAL_FAIL_OPEN, ["synthetic.py:3 _fail_open_or_deny: deny without a constant gate_id"]),
    ],
    ids=[
        "aliased-import",
        "partial-binding",
        "direct-writer",
        "async-def",
        "getattr-route",
        "renamed-reexport",
        "local-emitter",
        "local-fail-open",
    ],
)
def test_the_check_sees_a_deny_however_the_route_is_reached(source: str, expected: list[str]) -> None:
    problems, _count = gate_id_problems({"synthetic.py": source}, frozenset())

    assert problems == expected
