"""Static scan for management commands that bypass the machine-output seam.

``t3`` is a machine interface: a front-end shells to ``t3 ... --json`` and parses
stdout, so stdout must be a PURE data channel. :mod:`teatree.core.machine_output`
states the contract and ``emit`` is its one seam. Two shapes break it, and this
module detects both by walking the ``TyperCommand`` handlers' ASTs:

``JSON_FLAG_BYPASSES_SEAM``
    A handler declares ``--json`` yet hand-rolls the channel instead of calling
    ``emit`` — so its human view also lands on stdout, interleaved with the JSON.

``TYPED_RETURN_UNPINNED``
    A handler returns a structured payload without ``print_result = False``, so
    django-typer additionally ``str()``-es the return onto stdout: Python repr
    (single quotes, ``True``/``None``), which ``json.loads`` rejects.

``NON_STR_SCALAR_RETURN_UNPINNED``
    A handler returns a bare non-``str`` scalar without that pin. django-typer's own
    wrapper casts it, but ``call_command(..., stdout=)`` swaps in Django's, which
    calls ``.endswith`` on the raw value — so the return crashes whoever captured the
    output (souliane/teatree#4467). Only the pin makes that structurally impossible;
    the annotation is checked, so a value computed in a helper is caught too.

A handler that returns a bare ``str`` and declares no ``--json`` flag is NOT
reported: both wrappers write that return through unchanged, there is no machine
mode to protect, and adding one is a per-command product decision rather than a
mechanical conversion.

Detection follows delegation (bounded depth) so a handler whose whole body is
``_report(self, name, json_output=json_output)`` counts as routed when that
helper calls ``emit`` — the ``loop_state``/``loop_preset`` shape. Cross-module
delegation resolves only through a sibling-module alias (``_lanes.run_lanes``,
the ``e2e lanes`` shape); a bare ``self.x()`` never does, since attribute names
collide across the package and resolving them would absolve a real defect.
"""

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_HANDLER_DECORATORS = frozenset({"command", "initialize", "group"})
_SCALAR_RETURNS = frozenset({"str", "int", "bool", "float"})
_NON_STR_SCALARS = _SCALAR_RETURNS - {"str"}
_DELEGATION_DEPTH = 3


class DefectKind(StrEnum):
    JSON_FLAG_BYPASSES_SEAM = "json-flag-bypasses-seam"
    TYPED_RETURN_UNPINNED = "typed-return-unpinned"
    NON_STR_SCALAR_RETURN_UNPINNED = "non-str-scalar-return-unpinned"


@dataclass(frozen=True, order=True)
class SeamDefect:
    """One handler that breaks the stdout-is-a-data-channel contract."""

    module: str
    command_class: str
    handler: str
    kind: DefectKind

    @property
    def key(self) -> str:
        """The stable allowlist key — location-independent, so a moved handler keeps it."""
        return f"{self.module}:{self.command_class}.{self.handler}:{self.kind.value}"


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _is_handler(fn: ast.FunctionDef) -> bool:
    return fn.name == "handle" or any(_decorator_name(d) in _HANDLER_DECORATORS for d in fn.decorator_list)


def _called_names(fn: ast.FunctionDef) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _sibling_module_calls(fn: ast.FunctionDef, aliases: frozenset[str]) -> set[str]:
    """Names called as ``<sibling-module-alias>.<func>(...)`` — the cross-module delegations.

    Only these resolve against the package-wide index. A bare ``self.x()`` or
    ``Model.objects.remove()`` shares an attribute name with unrelated helpers
    across the package, so resolving those would silently absolve a real defect.
    """
    return {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in aliases
    }


def _sibling_module_aliases(module: ast.Module, stems: frozenset[str]) -> frozenset[str]:
    """Aliases bound to a sibling module of the package (matched by module stem, not path)."""
    return frozenset(
        alias.asname or alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in stems
    )


def _assigns_print_result(fn: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "print_result" for t in node.targets)
        for node in ast.walk(fn)
    )


def _declares_json_flag(fn: ast.FunctionDef) -> bool:
    params = [*fn.args.args, *fn.args.kwonlyargs]
    return any(p.annotation is not None and "--json" in ast.unparse(p.annotation) for p in params)


def _returns_structured(fn: ast.FunctionDef) -> bool:
    """True when django-typer would ``str()`` a structure (dict/dataclass/list) onto stdout."""
    if fn.returns is None:
        return False
    members = _union_members(fn.returns)
    concrete = [m for m in members if ast.unparse(m) != "None"]
    if not concrete:
        return False
    return not all(isinstance(m, ast.Name) and m.id in _SCALAR_RETURNS for m in concrete)


def _returns_non_str_scalar(fn: ast.FunctionDef) -> bool:
    """True when the annotation admits a non-``str`` scalar Django's wrapper would choke on."""
    if fn.returns is None:
        return False
    concrete = [m for m in _union_members(fn.returns) if ast.unparse(m) != "None"]
    return any(isinstance(m, ast.Name) and m.id in _NON_STR_SCALARS for m in concrete)


def _union_members(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*_union_members(node.left), *_union_members(node.right)]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # A stringified forward reference (``-> "RecordE2ERunResult"``).
        return _union_members(ast.parse(node.value, mode="eval").body)
    return [node]


class _CallGraph:
    """Functions reachable from a handler, so its delegation can be followed.

    Module-local names win over package-wide ones; the package-wide layer exists
    because a verb's body routinely lives in a sibling ``_<verb>.py`` helper
    module (the ``e2e lanes`` → ``_e2e_lanes.run_lanes`` shape).
    """

    def __init__(self, module: ast.Module, package: dict[str, ast.FunctionDef], stems: frozenset[str]) -> None:
        self._local: dict[str, ast.FunctionDef] = {}
        for node in ast.walk(module):
            if isinstance(node, ast.FunctionDef):
                self._local.setdefault(node.name, node)
        self._package = package
        self._aliases = _sibling_module_aliases(module, stems)

    def _callees(self, fn: ast.FunctionDef) -> dict[str, ast.FunctionDef]:
        resolved = {name: self._local[name] for name in _called_names(fn) if name in self._local}
        for name in _sibling_module_calls(fn, self._aliases):
            if name not in resolved and name in self._package:
                resolved[name] = self._package[name]
        return resolved

    def reaches(self, fn: ast.FunctionDef, *, predicate_name: str) -> bool:
        """True when *fn*, or a callee within the depth bound, matches."""
        seen: set[str] = set()
        frontier = [fn]
        for _ in range(_DELEGATION_DEPTH):
            next_frontier: list[ast.FunctionDef] = []
            for current in frontier:
                if predicate_name == "emit" and "emit" in _called_names(current):
                    return True
                if predicate_name == "print_result" and _assigns_print_result(current):
                    return True
                for name, callee in self._callees(current).items():
                    if name not in seen:
                        seen.add(name)
                        next_frontier.append(callee)
            frontier = next_frontier
        return False


def _class_chain_pins(cls: ast.ClassDef, classes: dict[str, ast.ClassDef]) -> bool:
    """True when *cls* or a base named in this package assigns ``print_result`` at class level."""
    seen: set[str] = set()
    frontier = [cls]
    while frontier:
        current = frontier.pop()
        if current.name in seen:
            continue
        seen.add(current.name)
        for stmt in current.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "print_result" for t in stmt.targets
            ):
                return True
        frontier.extend(classes[b.id] for b in current.bases if isinstance(b, ast.Name) and b.id in classes)
    return False


def _index_package(root: Path) -> tuple[dict[str, ast.ClassDef], dict[str, ast.FunctionDef]]:
    classes: dict[str, ast.ClassDef] = {}
    functions: dict[str, ast.FunctionDef] = {}
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, node)
            elif isinstance(node, ast.FunctionDef):
                functions.setdefault(node.name, node)
    return classes, functions


def scan_defects(root: Path) -> list[SeamDefect]:
    """Every handler under *root* that breaks the stdout-is-a-data-channel contract."""
    classes, functions = _index_package(root)
    return sorted(_iter_defects(root, classes, functions))


def _iter_defects(
    root: Path,
    classes: dict[str, ast.ClassDef],
    functions: dict[str, ast.FunctionDef],
) -> Iterator[SeamDefect]:
    stems = frozenset(p.stem for p in root.glob("*.py"))
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        index = _CallGraph(tree, functions, stems)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            pinned_by_class = _class_chain_pins(cls, classes)
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef) and _is_handler(n)]:
                routed = index.reaches(fn, predicate_name="emit")
                if _declares_json_flag(fn) and not routed:
                    yield SeamDefect(path.stem, cls.name, fn.name, DefectKind.JSON_FLAG_BYPASSES_SEAM)
                pinned = pinned_by_class or index.reaches(fn, predicate_name="print_result")
                if _returns_structured(fn):
                    if not pinned:
                        yield SeamDefect(path.stem, cls.name, fn.name, DefectKind.TYPED_RETURN_UNPINNED)
                elif _returns_non_str_scalar(fn) and not pinned:
                    yield SeamDefect(path.stem, cls.name, fn.name, DefectKind.NON_STR_SCALAR_RETURN_UNPINNED)


__all__ = ["DefectKind", "SeamDefect", "scan_defects"]
