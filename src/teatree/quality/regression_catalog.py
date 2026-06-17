"""Load the named regression-detector manifest from YAML into typed entries.

The source of truth is ``regression_rules.yaml`` next to this module; each entry
points at one ``.ast-grep/<status>/<id>.yml`` rule file. This mirrors
``catalog.py`` (the anti-pattern catalog loader): schema-validate at load time,
``RegressionCatalogError`` carries the offending entry id.

A ``blocking`` rule's bug is already fixed (zero findings on the current tree —
``tests/quality/test_regression_rules.py`` proves it), so its ``issue`` is the
``BLOCKING_NOW`` sentinel rather than a tracking issue. A ``warn`` rule's bug is
still open, so it MUST name a ``souliane/teatree#<n>`` tracking issue; the fix PR
flips it to blocking.

The detector engine is ast-grep (``teatree.quality.regression_scan``); each rule
file is a single-rule ast-grep YAML (``id`` + ``language`` + ``rule``).
"""

import dataclasses
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

import yaml

Status = Literal["blocking", "warn"]

BLOCKING_NOW = "BLOCKING-NOW"

_STATUSES: frozenset[str] = frozenset({"blocking", "warn"})
_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ISSUE_RE = re.compile(r"^souliane/teatree#\d+$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def manifest_path() -> Path:
    return Path(__file__).parent / "regression_rules.yaml"


class RegressionCatalogError(ValueError):
    def __init__(self, entry_id: str | None, message: str) -> None:
        loc = f"entry {entry_id!r}" if entry_id else "manifest"
        super().__init__(f"{loc}: {message}")


@dataclasses.dataclass(frozen=True)
class RegressionRule:
    id: str
    issue: str
    status: Status
    file: str

    @property
    def rule_path(self) -> Path:
        return repo_root() / self.file

    @property
    def is_blocking(self) -> bool:
        return self.status == "blocking"


def load_manifest(path: Path | None = None) -> tuple[RegressionRule, ...]:
    source = path or manifest_path()
    try:
        loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegressionCatalogError(None, str(exc)) from exc
    if not isinstance(loaded, list) or not loaded:
        raise RegressionCatalogError(None, "expected a top-level YAML list with at least one entry")
    rules = tuple(_parse_rule(item) for item in loaded)
    _check_unique_ids(rules)
    return rules


def load_astgrep_rule_ids(rule_path: Path) -> tuple[str, ...]:
    try:
        loaded = yaml.safe_load(rule_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegressionCatalogError(None, f"{rule_path}: invalid ast-grep YAML: {exc}") from exc
    if not isinstance(loaded, Mapping) or "rule" not in loaded:
        raise RegressionCatalogError(None, f"{rule_path}: ast-grep file must be a mapping with a 'rule' block")
    rule_id = loaded.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise RegressionCatalogError(None, f"{rule_path}: an ast-grep rule needs a string 'id'")
    return (rule_id,)


def _parse_rule(item: object) -> RegressionRule:
    if not isinstance(item, Mapping):
        raise RegressionCatalogError(None, f"each entry must be a mapping, got {type(item).__name__}")
    entry: Mapping[str, Any] = {str(k): v for k, v in item.items()}
    entry_id = _required_str(entry, "id", None)
    if not _ID_RE.match(entry_id):
        raise RegressionCatalogError(entry_id, "id must be a kebab slug (lowercase, digits, single hyphens)")
    status = cast("Status", _required_enum(entry, "status", _STATUSES, entry_id))
    issue = _required_str(entry, "issue", entry_id)
    _check_issue(issue, status, entry_id)
    file = _required_str(entry, "file", entry_id)
    _check_file_placement(file, status, entry_id)
    return RegressionRule(id=entry_id, issue=issue, status=status, file=file)


def _check_issue(issue: str, status: str, entry_id: str) -> None:
    if status == "blocking":
        if issue != BLOCKING_NOW:
            raise RegressionCatalogError(entry_id, f"a blocking rule's issue must be {BLOCKING_NOW!r}")
        return
    if not _ISSUE_RE.match(issue):
        raise RegressionCatalogError(entry_id, "a warn rule must name a souliane/teatree#<n> tracking issue")


def _check_file_placement(file: str, status: str, entry_id: str) -> None:
    expected_dir = f".ast-grep/{status}/"
    if not file.startswith(expected_dir):
        raise RegressionCatalogError(entry_id, f"a {status} rule's file must live under {expected_dir}")
    if not file.endswith(f"{entry_id}.yml"):
        raise RegressionCatalogError(entry_id, "the rule file name must match the entry id (<id>.yml)")


def _required_str(entry: Mapping[str, Any], key: str, entry_id: str | None) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegressionCatalogError(entry_id, f"required string field missing or empty: {key!r}")
    return value


def _required_enum(entry: Mapping[str, Any], key: str, allowed: frozenset[str], entry_id: str) -> str:
    value = _required_str(entry, key, entry_id)
    if value not in allowed:
        raise RegressionCatalogError(entry_id, f"{key} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _check_unique_ids(rules: tuple[RegressionRule, ...]) -> None:
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise RegressionCatalogError(rule.id, "duplicate id (ids must be unique and never reused)")
        seen.add(rule.id)
