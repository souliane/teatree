"""Turn common plain-word plan limits into diff checks with explicit failures."""

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from teatree.quality.changed_set import ChangedSet, is_migration

ConstraintKind = Literal["no_migration", "no_setting", "no_public_name", "unchanged_file"]


@dataclass(frozen=True, slots=True)
class BriefConstraint:
    kind: ConstraintKind
    target: str
    source: str


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    kind: ConstraintKind
    target: str
    detail: str


_NEGATIVE = re.compile(r"\b(?:no|do\s+not|don't|must\s+not|without)\b([^.;\n]{0,120})", re.IGNORECASE)
_EXAMPLE_GROUP = re.compile(r"\b(?:closed\s+grammar|for\s+example|e\.g\.|such\s+as)\s*\([^()\n]*\)", re.IGNORECASE)
_BLOCK_QUOTE = re.compile(r"^[ \t]*>[^\n]*$", re.MULTILINE)
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|(?<!\w)\'[^\'\n]*\'|\u2018[^\u2019\n]*\u2019')
_ATTRIBUTED = re.compile(
    r"\b(?:(?:another|other|external)\s+(?:issue|pull\s+request|pr|mr)|"
    r"(?:issue|pull\s+request|pr|mr)\s*#\d+)\b[^.;\n]{0,100}?"
    r"\b(?:says?|said|states?|asks?|quotes?)\b[^.;\n]*",
    re.IGNORECASE,
)
_ANOTHER_REQUIRES = re.compile(
    r"\b(?:another|other|external)\s+(?:issue|pull\s+request|pr|mr)\b[^.;\n]{0,100}?"
    r"\brequires?\b[^.;\n]*",
    re.IGNORECASE,
)
_PATH = r"([\w./-]+\.(?:py|md|toml|yaml|yml|json|cfg|ini))"
_KEEP = re.compile(rf"\b(?:keep|leave)\s+{_PATH}\s+unchanged\b", re.IGNORECASE)
_STAYS = re.compile(rf"\b{_PATH}\s+(?:stays?|must\s+remain)\s+unchanged\b", re.IGNORECASE)
_DO_NOT_TOUCH = re.compile(rf"\b(?:do\s+not|don't)\s+(?:change|touch|edit)\s+{_PATH}\b", re.IGNORECASE)


def extract_constraints(text: str) -> tuple[BriefConstraint, ...]:
    text = _EXAMPLE_GROUP.sub("", text)
    text = _BLOCK_QUOTE.sub("", text)
    text = _QUOTED.sub(".", text)
    text = _ATTRIBUTED.sub("", text)
    text = _ANOTHER_REQUIRES.sub("", text)
    found: dict[tuple[ConstraintKind, str], BriefConstraint] = {}
    for match in _NEGATIVE.finditer(text):
        scope = match.group(1).lower()
        if scope.lstrip().startswith("forget to "):
            continue
        scope = re.sub(r"(?<![\w-])--settings(?=\s|=|$)", "", scope)
        for kind, pattern in (
            ("no_migration", r"\bmigrations?\b"),
            ("no_setting", r"\bsettings?\b"),
            ("no_public_name", r"\bpublic\s+(?:names?|api|symbols?)\b"),
        ):
            if re.search(pattern, scope):
                found[kind, ""] = BriefConstraint(kind, "", match.group(0).strip())
    for pattern in (_KEEP, _STAYS, _DO_NOT_TOUCH):
        for match in pattern.finditer(text):
            target = match.group(1).removeprefix("./")
            found["unchanged_file", target] = BriefConstraint("unchanged_file", target, match.group(0))
    return tuple(found[key] for key in sorted(found))


def _public_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {name for name in names if not name.startswith("_")}


def _setting_fields(source: str) -> set[str]:
    tree = ast.parse(source)
    fields = {
        f"{node.name}.{field.target.id}"
        for node in tree.body
        if isinstance(node, ast.ClassDef) and "settings" in node.name.lower()
        for field in node.body
        if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
    }
    registries = {"REGISTRY_SETTINGS", "COLD_SETTINGS", "COLD_HOOK_SETTINGS", "OVERLAY_OVERRIDABLE_SETTINGS"}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        if isinstance(value, ast.Dict):
            for target in targets:
                if target in registries:
                    fields.update(
                        f"{target}.{key.value}"
                        for key in value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
    return fields


def _source_violation(
    constraint: BriefConstraint,
    changed: ChangedSet,
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> ConstraintViolation | None:
    paths = sorted(
        {
            entry.path
            for entry in changed.entries
            if entry.path.startswith("src/teatree/") and entry.path.endswith(".py")
        }
    )
    for path in paths:
        old = before.get(path)
        new = after.get(path)
        if old is None or new is None:
            return ConstraintViolation(constraint.kind, path, f"source unavailable for {path}; constraint unverified")
        try:
            old_names = _setting_fields(old) if constraint.kind == "no_setting" else _public_names(old)
            new_names = _setting_fields(new) if constraint.kind == "no_setting" else _public_names(new)
        except SyntaxError:
            return ConstraintViolation(
                constraint.kind, path, f"source could not be parsed for {path}; constraint unverified"
            )
        if added := sorted(new_names - old_names):
            return ConstraintViolation(constraint.kind, path, f"{path} adds {', '.join(added)}")
    return None


def check_constraints(
    constraints: tuple[BriefConstraint, ...],
    changed: ChangedSet,
    *,
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> tuple[ConstraintViolation, ...]:
    paths = {entry.path for entry in changed.entries}
    violations: list[ConstraintViolation] = []
    for constraint in constraints:
        if constraint.kind == "no_migration":
            migrations = sorted(path for path in paths if is_migration(path))
            if migrations:
                violations.append(
                    ConstraintViolation(constraint.kind, migrations[0], f"migration changed: {migrations[0]}")
                )
        elif constraint.kind == "unchanged_file":
            if constraint.target in paths:
                violations.append(
                    ConstraintViolation(constraint.kind, constraint.target, f"required unchanged: {constraint.target}")
                )
        elif violation := _source_violation(constraint, changed, before, after):
            violations.append(violation)
    return tuple(violations)
