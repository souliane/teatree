"""Load the architectural anti-pattern catalog from YAML into typed entries.

The source of truth is ``antipatterns.yaml`` next to this module. Each entry is
validated at load time; :class:`CatalogError` carries the offending entry id so
authors can jump to the problem.

The ``detection`` vocabulary mirrors the ``Confidence`` literal in
``teatree.eval.transcript_conformance``: ``greppable`` ≙ ``deterministic`` (a
regex over the diff decides it) and ``judgement`` ≙ ``judgement`` (a reviewer
decides it). A ``grep_hint`` is REQUIRED for ``greppable`` entries and forbidden
for ``judgement`` ones — the regex is what makes a greppable entry mechanizable.
"""

import dataclasses
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

import yaml

Severity = Literal["high", "medium", "low"]
Detection = Literal["greppable", "judgement"]

_SEVERITIES: frozenset[str] = frozenset({"high", "medium", "low"})
_DETECTIONS: frozenset[str] = frozenset({"greppable", "judgement"})
_CONSUMERS: frozenset[str] = frozenset({"architecture-design", "ac-reviewing-codebase", "linter", "eval"})
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def catalog_path() -> Path:
    return Path(__file__).parent / "antipatterns.yaml"


class CatalogError(ValueError):
    def __init__(self, entry_id: str | None, message: str) -> None:
        loc = f"entry {entry_id!r}" if entry_id else "catalog"
        super().__init__(f"{loc}: {message}")


@dataclasses.dataclass(frozen=True)
class AntiPatternEntry:
    """One architectural anti-pattern, loaded from a YAML mapping.

    ``grep_hint`` is the diff-scanning regex; it is present iff
    ``detection == "greppable"``. ``linter`` names a ``scripts/hooks/*.py`` (or
    a real tool like ``tach``) that mechanizes the check, or ``None`` to leave
    the enforcement gap visible. ``eval_invariant`` links to a
    ``transcript_conformance`` invariant id, or ``None``.
    """

    id: str
    name: str
    severity: Severity
    detection: Detection
    anti_pattern: str
    preferred_pattern: str
    consumers: tuple[str, ...]
    refs: tuple[str, ...]
    grep_hint: str | None = None
    linter: str | None = None
    eval_invariant: str | None = None


def load_catalog(path: Path | None = None) -> tuple[AntiPatternEntry, ...]:
    source = path or catalog_path()
    return load_catalog_text(source.read_text(encoding="utf-8"))


def load_catalog_text(text: str) -> tuple[AntiPatternEntry, ...]:
    """Parse catalog YAML from an in-memory string.

    The path-free entry point lets a drift gate render the catalog from the
    YAML as it sits in the git index (``git show :<path>``) rather than the
    working tree, so an unstaged edit cannot mask committed drift.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(None, str(exc)) from exc
    if not isinstance(loaded, list) or not loaded:
        raise CatalogError(None, "expected a top-level YAML list with at least one entry")
    entries = tuple(_parse_entry(item) for item in loaded)
    _check_unique_ids(entries)
    _check_unique_grep_hints(entries)
    return entries


def _parse_entry(item: object) -> AntiPatternEntry:
    if not isinstance(item, Mapping):
        raise CatalogError(None, f"each entry must be a mapping, got {type(item).__name__}")
    entry: Mapping[str, Any] = {str(k): v for k, v in item.items()}
    entry_id = _required_str(entry, "id", None)
    if not _ID_RE.match(entry_id):
        raise CatalogError(entry_id, "id must be a kebab slug (lowercase, digits, single hyphens)")
    detection = cast("Detection", _required_enum(entry, "detection", _DETECTIONS, entry_id))
    grep_hint = _parse_grep_hint(entry, detection, entry_id)
    return AntiPatternEntry(
        id=entry_id,
        name=_required_str(entry, "name", entry_id),
        severity=cast("Severity", _required_enum(entry, "severity", _SEVERITIES, entry_id)),
        detection=detection,
        anti_pattern=_required_str(entry, "anti_pattern", entry_id),
        preferred_pattern=_required_str(entry, "preferred_pattern", entry_id),
        consumers=_parse_consumers(entry, entry_id),
        refs=_parse_str_list(entry, "refs", entry_id),
        grep_hint=grep_hint,
        linter=_parse_optional_str(entry, "linter", entry_id),
        eval_invariant=_parse_optional_str(entry, "eval_invariant", entry_id),
    )


def _parse_grep_hint(entry: Mapping[str, Any], detection: str, entry_id: str) -> str | None:
    raw = entry.get("grep_hint")
    if detection == "greppable":
        if not isinstance(raw, str) or not raw.strip():
            raise CatalogError(entry_id, "greppable entry requires a non-empty grep_hint")
        try:
            re.compile(raw)
        except re.error as exc:
            raise CatalogError(entry_id, f"grep_hint is not a valid regex: {exc}") from exc
        return raw
    if raw is not None:
        raise CatalogError(entry_id, "grep_hint is forbidden on a judgement entry")
    return None


def _parse_consumers(entry: Mapping[str, Any], entry_id: str) -> tuple[str, ...]:
    values = _parse_str_list(entry, "consumers", entry_id)
    if not values:
        raise CatalogError(entry_id, "consumers must be a non-empty list")
    unknown = [v for v in values if v not in _CONSUMERS]
    if unknown:
        raise CatalogError(entry_id, f"unknown consumer(s): {', '.join(unknown)}")
    return values


def _parse_str_list(entry: Mapping[str, Any], key: str, entry_id: str) -> tuple[str, ...]:
    raw = entry.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise CatalogError(entry_id, f"{key} must be a list of strings")
    if not all(isinstance(v, str) and v.strip() for v in raw):
        raise CatalogError(entry_id, f"{key} must be a list of non-empty strings")
    return tuple(str(v) for v in raw)


def _parse_optional_str(entry: Mapping[str, Any], key: str, entry_id: str) -> str | None:
    raw = entry.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError(entry_id, f"{key} must be a non-empty string or null")
    return raw


def _required_str(entry: Mapping[str, Any], key: str, entry_id: str | None) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(entry_id, f"required string field missing or empty: {key!r}")
    return value


def _required_enum(entry: Mapping[str, Any], key: str, allowed: frozenset[str], entry_id: str) -> str:
    value = _required_str(entry, key, entry_id)
    if value not in allowed:
        raise CatalogError(entry_id, f"{key} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _check_unique_ids(entries: tuple[AntiPatternEntry, ...]) -> None:
    seen: set[str] = set()
    for entry in entries:
        if entry.id in seen:
            raise CatalogError(entry.id, "duplicate id (ids must be unique and never reused)")
        seen.add(entry.id)


def _check_unique_grep_hints(entries: tuple[AntiPatternEntry, ...]) -> None:
    by_hint: dict[str, str] = {}
    for entry in entries:
        if entry.grep_hint is None:
            continue
        prior = by_hint.get(entry.grep_hint)
        if prior is not None:
            raise CatalogError(entry.id, f"grep_hint duplicates the one on entry {prior!r}")
        by_hint[entry.grep_hint] = entry.id
