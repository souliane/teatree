"""The pre-push leak hook refuses to run code that differs from HEAD, so the list of that code must be complete.

The hook runs ``scripts/privacy_scan.py``, the visibility resolver and ``remote_holds`` from the working tree it
lives in, and an uncommitted edit of any file they import would be judged by the edit. It reads
``scripts/hooks/leak-gate-sources.txt`` from HEAD and compares each listed file with its HEAD blob, so a module
missing from the list is a module a pusher can edit unseen. This pins the list to the import closure, read from
the source with ``ast`` (function-level imports included, which a run of the scanner would not exercise).
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
LIST = ROOT / "scripts" / "hooks" / "leak-gate-sources.txt"
_ROOTS = (
    "scripts/privacy_scan.py",
    "src/teatree/hooks/repo_visibility_cli.py",
    "src/teatree/hooks/remote_holds.py",
)


def _module_file(name: str) -> Path | None:
    base = SRC.joinpath(*name.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return base.with_suffix(".py") if base.with_suffix(".py").is_file() else None


def _teatree_imports(path: Path) -> set[str]:
    package = ".".join(path.relative_to(SRC).with_suffix("").parts[:-1]) if path.is_relative_to(SRC) else ""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.startswith("teatree"))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join(part for part in [*anchor, base] if part)
            if base.startswith("teatree"):
                names.add(base)
                names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _closure() -> set[str]:
    seen: set[Path] = set()
    pending = [ROOT / relative for relative in _ROOTS]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for name in _teatree_imports(current):
            parts = name.split(".")
            for depth in range(1, len(parts) + 1):
                module = _module_file(".".join(parts[:depth]))
                if module is not None and module not in seen:
                    pending.append(module)
    return {path.relative_to(ROOT).as_posix() for path in seen}


def _listed() -> set[str]:
    return {line for line in LIST.read_text(encoding="utf-8").splitlines() if line}


def test_the_list_is_the_import_closure_of_what_the_hook_runs() -> None:
    closure, listed = _closure(), _listed()

    assert sorted(closure - listed) == [], "run by the hook but unguarded: add to leak-gate-sources.txt"
    assert sorted(listed - closure) == [], "listed but not run by the hook: remove from leak-gate-sources.txt"


def test_the_list_is_sorted_and_free_of_duplicates() -> None:
    lines = [line for line in LIST.read_text(encoding="utf-8").splitlines() if line]

    assert lines == sorted(set(lines))
