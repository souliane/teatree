"""What a module's SOURCE binds, for the names its runtime namespace does not carry.

:mod:`teatree.quality.skill_symbol_refs` resolves a dotted citation by importing the
module and walking ``getattr``. Three scopes are invisible to that walk, and each one
reported a CORRECT reference as rot (#4448):

- a class member — ``importer._copy_ref_to_ticket`` is a method, not a module attribute
- a ``TYPE_CHECKING``-only import, absent at runtime by construction
- an inherited dataclass field, whose ``default_factory`` leaves no class attribute and
    whose annotation sits on a base rather than the class the citation names

Each lookup is deliberately narrow, because a resolver that resolves too much stops
catching the moved-symbol rot the guard exists for: only a class the module DEFINES can
vouch for a member, and a ``TYPE_CHECKING`` name is resolved at its real home rather than
accepted on sight — so a guarded import of a symbol that has since moved still reds.
"""

import ast
import importlib
import inspect
from collections.abc import Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType, ModuleType

_TYPE_CHECKING = "TYPE_CHECKING"
_STAR = "*"


def annotated_in_mro(cls: type, name: str) -> bool:
    """Whether any class in *cls*'s MRO annotates *name*.

    ``cls.__annotations__`` is an ordinary attribute lookup, so a class carrying any
    annotations of its own shadows every base's — which is how an inherited dataclass
    field read as absent from the very class that inherits it.
    """
    return any(name in vars(base).get("__annotations__", {}) for base in cls.__mro__)


def module_class_members(module: ModuleType) -> frozenset[str]:
    """Own member names of the classes *module* DEFINES."""
    return frozenset(name for cls in _defined_classes(module) for name in vars(cls))


def _defined_classes(module: ModuleType) -> Iterator[type]:
    """The classes *module* declares, in name order.

    The ``__module__`` filter is load-bearing: an imported class must never vouch for a
    citation, or every module re-exporting a class would answer for its members too.
    """
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if cls.__module__ == module.__name__:
            yield cls


def type_checking_bindings(module_file: Path, package: str) -> Mapping[str, str]:
    """Names bound only under ``if TYPE_CHECKING:``, mapped to their fully-dotted home.

    *package* resolves relative imports; pass the module's ``__package__``. A source that
    will not parse binds nothing rather than raising — an unreadable module is the walk's
    problem, not this lookup's. The parse is cached against the file's mtime, so a path
    rewritten in place re-parses instead of serving the previous file's bindings.
    """
    try:
        stamp = module_file.stat().st_mtime_ns
    except OSError:
        return MappingProxyType({})
    return _parse_type_checking_bindings(module_file, stamp, package)


@lru_cache(maxsize=1024)
def _parse_type_checking_bindings(module_file: Path, _mtime_ns: int, package: str) -> Mapping[str, str]:
    # `_mtime_ns` is a cache-key discriminator only — it is what makes a rewritten path re-parse.
    try:
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return MappingProxyType({})
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _guards_type_checking(node.test):
            bindings.update(_guarded_bindings(node, package))
    return MappingProxyType(bindings)


def source_bound_object(module: ModuleType, name: str) -> object | None:
    """The object *module*'s source binds under *name*, when its namespace does not."""
    module_file = getattr(module, "__file__", None)
    if module_file is not None:
        origin = type_checking_bindings(Path(module_file), module.__package__ or "").get(name)
        if origin is not None:
            return _imported_attribute(origin)
    for cls in _defined_classes(module):
        if name in vars(cls):
            return vars(cls)[name]
    return None


def _guards_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == _TYPE_CHECKING
    return isinstance(test, ast.Attribute) and test.attr == _TYPE_CHECKING


def _guarded_bindings(node: ast.If, package: str) -> Iterator[tuple[str, str]]:
    # Only `body` — an `else:` branch of a TYPE_CHECKING guard is the RUNTIME path.
    for statement in node.body:
        for sub in ast.walk(statement):
            if isinstance(sub, ast.Import):
                yield from _plain_import_bindings(sub)
            elif isinstance(sub, ast.ImportFrom):
                yield from _from_import_bindings(sub, package)


def _plain_import_bindings(node: ast.Import) -> Iterator[tuple[str, str]]:
    for alias in node.names:
        if alias.asname:
            yield alias.asname, alias.name
        else:
            head = alias.name.partition(".")[0]
            yield head, head


def _from_import_bindings(node: ast.ImportFrom, package: str) -> Iterator[tuple[str, str]]:
    origin = _absolute_module(node.module, node.level, package)
    if origin is None:
        return
    for alias in node.names:
        if alias.name != _STAR:
            yield alias.asname or alias.name, f"{origin}.{alias.name}"


def _absolute_module(module: str | None, level: int, package: str) -> str | None:
    if not level:
        return module
    base = package
    for _ in range(level - 1):
        base = base.rpartition(".")[0]
    if not base:
        return None
    return f"{base}.{module}" if module else base


def _imported_attribute(dotted: str) -> object | None:
    """The object a fully-dotted name denotes — the sibling of ``resolve_dotted``'s reason."""
    parts = dotted.split(".")
    for depth in range(len(parts), 0, -1):
        try:
            found: object = importlib.import_module(".".join(parts[:depth]))
        except ImportError:
            continue
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        for name in parts[depth:]:
            if not hasattr(found, name):
                return None
            found = getattr(found, name)
        return found
    return None
