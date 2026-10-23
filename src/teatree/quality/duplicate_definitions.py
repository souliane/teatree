"""Find definitions shadowed by a later definition in a changed Python file."""

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DuplicateDefinition:
    path: str
    name: str
    first_line: int
    second_line: int


def _is_overload(node: ast.AST) -> bool:
    decorators = getattr(node, "decorator_list", ())
    return any(
        (isinstance(decorator, ast.Name) and decorator.id == "overload")
        or (isinstance(decorator, ast.Attribute) and decorator.attr == "overload")
        for decorator in decorators
    )


def _is_setter(node: ast.AST) -> bool:
    decorators = getattr(node, "decorator_list", ())
    return any(
        isinstance(decorator, ast.Attribute) and decorator.attr in {"setter", "deleter"} for decorator in decorators
    )


def _scan_body(path: str, body: list[ast.stmt], prefix: str = "") -> list[DuplicateDefinition]:
    seen: dict[str, ast.stmt] = {}
    findings: list[DuplicateDefinition] = []
    for node in body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        name = f"{prefix}{node.name}"
        first = seen.get(node.name)
        if first is not None and not _is_overload(first) and not _is_overload(node) and not _is_setter(node):
            findings.append(DuplicateDefinition(path, name, first.lineno, node.lineno))
        seen[node.name] = node
        if isinstance(node, ast.ClassDef):
            findings.extend(_scan_body(path, node.body, f"{name}."))
    return findings


def duplicate_definitions(repo_root: Path, paths: tuple[Path, ...]) -> tuple[DuplicateDefinition, ...]:
    findings: list[DuplicateDefinition] = []
    for relative in sorted(set(paths)):
        if relative.suffix != ".py":
            continue
        if relative.is_absolute() or ".." in relative.parts:
            message = f"changed path leaves repository: {relative}"
            raise ValueError(message)
        path = repo_root / relative
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
        findings.extend(_scan_body(relative.as_posix(), tree.body))
    return tuple(findings)
