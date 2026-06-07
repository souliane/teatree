"""Resolve string-based ``mock.patch`` targets against the live module tree.

A ``patch("old.dotted.path")`` or ``patch.object(module_alias, "attr")`` left
after a module move applies to a DEAD name, so the test patches nothing and
passes vacuously. The import-based rename sweep cannot see a string target. This
module statically extracts every *resolvable* patch string target from a test
AST and resolves it the same way :mod:`unittest.mock` does at patch-application
time — :func:`pkgutil.resolve_name` on the module-and-object prefix, then
``getattr`` for the patched leaf — so the gate's verdict matches what ``patch()``
would actually do.

What counts as *resolvable* (and therefore checked):

- ``patch("a.b.c")`` / ``mock.patch("a.b.c")`` — a constant dotted string.
- ``patch.object(alias, "attr")`` where ``alias`` is a module imported at the
    file's top level (``import a.b as alias`` or ``from a import alias`` where
    the imported name is itself a module). The alias is canonicalised UP to its
    dotted module path, then ``alias.attr`` is a normal dotted target.

What is exempt (declared-dynamic, never flagged):

- ``create=True`` — the call declares it creates the attribute, so a missing
    attr is intended.
- A non-constant first argument (a variable, an f-string, an attribute access)
    — the target is built at runtime and cannot be resolved statically.
- A ``patch.object`` first argument that is not a top-level module alias (a
    class, a fixture-created object, a local variable).
- A line carrying the ``# patch-target: dynamic`` pragma — the narrow, explicit
    allowlist for a genuinely dynamic target.
"""

import ast
import importlib
import importlib.util
import pkgutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DYNAMIC_PRAGMA = "patch-target: dynamic"
_PATCH_NAMES = frozenset({"patch"})
_PATCH_OBJECT_ARITY = 2


@dataclass(frozen=True)
class PatchTargetFinding:
    """One resolvable string-based patch target found in a file.

    ``reason`` is ``None`` when the target resolves against the live tree, and a
    human-readable failure string (the resolution exception) when it does not.
    """

    path: Path
    lineno: int
    target: str
    reason: str | None


def resolve_patch_target(dotted: str) -> str | None:
    """Resolve a dotted patch target; return ``None`` if it resolves, else why not.

    Mirrors :func:`unittest.mock._get_target`: the target splits into a
    ``rsplit('.', 1)`` prefix resolved via :func:`pkgutil.resolve_name` and a
    leaf attribute fetched with ``getattr``. A bare module path (no dot) is
    resolved as-is.
    """
    prefix, _, attribute = dotted.rpartition(".")
    try:
        if prefix:
            obj = pkgutil.resolve_name(prefix)
            getattr(obj, attribute)
        else:
            importlib.import_module(dotted)
    except (ImportError, AttributeError, ValueError, TypeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Map top-level names bound to a module to that module's dotted path.

    ``import a.b as alias`` → ``{"alias": "a.b"}``;
    ``import a.b`` → ``{"a": "a", "a.b": "a.b"}`` (the bound name is ``a``);
    ``from a import b`` → ``{"b": "a.b"}`` only when ``a.b`` is itself a module.
    """
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.asname:
                    aliases[name.asname] = name.name
                else:
                    aliases[name.name.split(".", 1)[0]] = name.name.split(".", 1)[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for name in node.names:
                candidate = f"{node.module}.{name.name}"
                if _is_module(candidate):
                    aliases[name.asname or name.name] = candidate
    return aliases


def _is_module(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _is_patch_call(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id in _PATCH_NAMES
    if isinstance(func, ast.Attribute):
        return func.attr in _PATCH_NAMES
    return False


def _is_patch_object_call(func: ast.expr) -> bool:
    return isinstance(func, ast.Attribute) and func.attr == "object"


def _has_create_true(call: ast.Call) -> bool:
    return any(
        kw.arg == "create" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in call.keywords
    )


def _const_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dynamic_pragma_lines(source: str) -> set[int]:
    return {lineno for lineno, line in enumerate(source.splitlines(), start=1) if DYNAMIC_PRAGMA in line}


def _targets_in_call(call: ast.Call, aliases: dict[str, str]) -> str | None:
    if _has_create_true(call) or not call.args:
        return None
    if _is_patch_object_call(call.func):
        return _patch_object_target(call, aliases)
    if _is_patch_call(call.func):
        return _const_str(call.args[0])
    return None


def _patch_object_target(call: ast.Call, aliases: dict[str, str]) -> str | None:
    if len(call.args) < _PATCH_OBJECT_ARITY:
        return None
    attr = _const_str(call.args[1])
    module = _alias_to_module(call.args[0], aliases)
    if attr is None or module is None:
        return None
    return f"{module}.{attr}"


def _alias_to_module(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    return None


def scan_source(source: str, path: Path) -> list[PatchTargetFinding]:
    """Extract and resolve every resolvable patch string target in ``source``."""
    tree = ast.parse(source, filename=str(path))
    aliases = _module_aliases(tree)
    pragma_lines = _dynamic_pragma_lines(source)
    findings: list[PatchTargetFinding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _targets_in_call(node, aliases)
        if target is None or node.lineno in pragma_lines:
            continue
        findings.append(
            PatchTargetFinding(
                path=path,
                lineno=node.lineno,
                target=target,
                reason=resolve_patch_target(target),
            )
        )
    return findings


def scan_file(path: Path) -> list[PatchTargetFinding]:
    source = path.read_text(encoding="utf-8")
    return scan_source(source, path)


def scan_tree(roots: Iterable[Path], *, suffixes: Sequence[str] = (".py",)) -> list[PatchTargetFinding]:
    findings: list[PatchTargetFinding] = []
    for root in roots:
        if not root.is_dir():
            continue
        for suffix in suffixes:
            for path in sorted(root.rglob(f"*{suffix}")):
                findings.extend(scan_file(path))
    return findings
