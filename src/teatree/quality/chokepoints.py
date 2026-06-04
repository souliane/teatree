"""Load the chokepoint registry from YAML into typed entries.

The source of truth is ``chokepoints.yaml`` next to this module. Each entry maps
a dangerous/privileged call symbol to the SOLE module(s) allowed to call it — a
call-site authorization manifest. One generic AST checker
(``scripts/hooks/check_chokepoints.py``) reads this registry and fails when a
protected symbol is called from a module outside its ``allowed_modules``.

This mirrors ``catalog.py`` / ``regression_catalog.py``: schema-validate at load
time, :class:`ChokepointError` carries the offending entry id. It is orthogonal
to tach (the import graph) and semgrep (intra-body code shapes) — this layer
owns call-site authorization only.

``match_kind`` is the only DSL and has exactly two values. ``module_attr`` is a
module-qualified attribute call whose receiver is a bare ``Name`` equal to
``protected_symbol`` (e.g. ``subprocess.run(...)``). ``method`` is an attribute
call matched by ``.attr`` alone, any receiver (e.g. ``x.post_routed(...)``);
``exempt_receivers`` whitelists receivers that route through the chokepoint
itself so they never count as a bypass.
"""

import dataclasses
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

import yaml

MatchKind = Literal["module_attr", "method"]

_MATCH_KINDS: frozenset[str] = frozenset({"module_attr", "method"})
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MODULE_RE = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*$")


def registry_path() -> Path:
    return Path(__file__).parent / "chokepoints.yaml"


class ChokepointError(ValueError):
    def __init__(self, entry_id: str | None, message: str) -> None:
        loc = f"entry {entry_id!r}" if entry_id else "registry"
        super().__init__(f"{loc}: {message}")


@dataclasses.dataclass(frozen=True)
class Chokepoint:
    id: str
    name: str
    concern: str
    match_kind: MatchKind
    protected_attrs: tuple[str, ...]
    allowed_modules: tuple[str, ...]
    protected_symbol: str = ""
    exempt_receivers: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()

    def allows(self, module_path: str) -> bool:
        return module_path in self.allowed_modules


def load_registry(path: Path | None = None) -> tuple[Chokepoint, ...]:
    source = path or registry_path()
    try:
        loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ChokepointError(None, str(exc)) from exc
    if not isinstance(loaded, list) or not loaded:
        raise ChokepointError(None, "expected a top-level YAML list with at least one entry")
    entries = tuple(_parse_entry(item) for item in loaded)
    _check_unique_ids(entries)
    return entries


def _parse_entry(item: object) -> Chokepoint:
    if not isinstance(item, Mapping):
        raise ChokepointError(None, f"each entry must be a mapping, got {type(item).__name__}")
    entry: Mapping[str, Any] = {str(k): v for k, v in item.items()}
    entry_id = _required_str(entry, "id", None)
    if not _ID_RE.match(entry_id):
        raise ChokepointError(entry_id, "id must be a kebab slug (lowercase, digits, single hyphens)")
    match_kind = cast("MatchKind", _required_enum(entry, "match_kind", _MATCH_KINDS, entry_id))
    protected_attrs = _parse_str_list(entry, "protected_attrs", entry_id)
    if not protected_attrs:
        raise ChokepointError(entry_id, "protected_attrs must be a non-empty list")
    allowed_modules = _parse_str_list(entry, "allowed_modules", entry_id)
    if not allowed_modules:
        raise ChokepointError(entry_id, "allowed_modules must be a non-empty list")
    _check_module_paths(allowed_modules, entry_id)
    protected_symbol = _parse_protected_symbol(entry, match_kind, entry_id)
    return Chokepoint(
        id=entry_id,
        name=_required_str(entry, "name", entry_id),
        concern=_required_str(entry, "concern", entry_id),
        match_kind=match_kind,
        protected_attrs=protected_attrs,
        allowed_modules=allowed_modules,
        protected_symbol=protected_symbol,
        exempt_receivers=_parse_str_list(entry, "exempt_receivers", entry_id),
        refs=_parse_str_list(entry, "refs", entry_id),
    )


def _parse_protected_symbol(entry: Mapping[str, Any], match_kind: str, entry_id: str) -> str:
    raw = entry.get("protected_symbol")
    if match_kind == "module_attr":
        if not isinstance(raw, str) or not raw.strip():
            raise ChokepointError(entry_id, "module_attr entry requires a non-empty protected_symbol")
        return raw
    if raw is not None:
        raise ChokepointError(entry_id, "protected_symbol is forbidden on a method entry")
    return ""


def _check_module_paths(modules: tuple[str, ...], entry_id: str) -> None:
    for module in modules:
        if not _MODULE_RE.match(module):
            raise ChokepointError(entry_id, f"allowed_modules entry is not a dotted module path: {module!r}")


def _parse_str_list(entry: Mapping[str, Any], key: str, entry_id: str | None) -> tuple[str, ...]:
    raw = entry.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise ChokepointError(entry_id, f"{key} must be a list of strings")
    if not all(isinstance(v, str) and v.strip() for v in raw):
        raise ChokepointError(entry_id, f"{key} must be a list of non-empty strings")
    return tuple(str(v) for v in raw)


def _required_str(entry: Mapping[str, Any], key: str, entry_id: str | None) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ChokepointError(entry_id, f"required string field missing or empty: {key!r}")
    return value


def _required_enum(entry: Mapping[str, Any], key: str, allowed: frozenset[str], entry_id: str) -> str:
    value = _required_str(entry, key, entry_id)
    if value not in allowed:
        raise ChokepointError(entry_id, f"{key} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _check_unique_ids(entries: tuple[Chokepoint, ...]) -> None:
    seen: set[str] = set()
    for entry in entries:
        if entry.id in seen:
            raise ChokepointError(entry.id, "duplicate id (ids must be unique and never reused)")
        seen.add(entry.id)
