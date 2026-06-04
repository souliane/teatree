"""Generic chokepoint checker: call-site authorization across src/teatree.

Registry-driven (``src/teatree/quality/chokepoints.yaml``): for each protected
symbol, fail when it is called from a module outside its ``allowed_modules``.
One AST visitor replaces the per-feature ``check_subprocess_ban.py`` and the
bespoke on-behalf import-guard test.

Scope is ``src/teatree/`` only — tests are never scanned. ``def`` statements are
never matched (only ``ast.Call`` is inspected), so a protected method's own
definition and Protocol stubs are inherently safe. Diff-scoped via filename args
in pre-commit; ``--all`` walks the whole tree (the CI / conformance path).
"""

import argparse
import ast
import pathlib
import sys

from teatree.quality.chokepoints import Chokepoint, load_registry

TARGET_PREFIXES: tuple[str, ...] = ("src/teatree/",)


def module_path_for(rel: str) -> str:
    parts = pathlib.PurePosixPath(rel.removeprefix("src/")).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _receiver_name(node: ast.Attribute) -> str | None:
    receiver = node.value
    if isinstance(receiver, ast.Name):
        return receiver.id
    if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name):
        return receiver.func.id
    return None


def _matches(node: ast.Call, entry: Chokepoint) -> str | None:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in entry.protected_attrs:
        return None
    if entry.match_kind == "module_attr":
        if not (isinstance(func.value, ast.Name) and func.value.id == entry.protected_symbol):
            return None
        return f"{entry.protected_symbol}.{func.attr}(...)"
    if _receiver_name(func) in entry.exempt_receivers:
        return None
    return f"{func.attr}(...)"


def _scan_file(
    path: pathlib.Path, module_path: str, registry: tuple[Chokepoint, ...]
) -> list[tuple[int, Chokepoint, str]]:
    active = tuple(entry for entry in registry if not entry.allows(module_path))
    if not active:
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    hits: list[tuple[int, Chokepoint, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for entry in active:
            what = _matches(node, entry)
            if what is not None:
                hits.append((node.lineno, entry, what))
    return hits


def _normalize(path_arg: str) -> str:
    p = pathlib.Path(path_arg)
    try:
        return str(p.resolve().relative_to(pathlib.Path.cwd())).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _in_scope(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in TARGET_PREFIXES)


def _iter_tree() -> list[str]:
    root = pathlib.Path("src/teatree")
    return [str(p).replace("\\", "/") for p in sorted(root.rglob("*.py"))]


def check(paths: list[str], registry: tuple[Chokepoint, ...] | None = None) -> int:
    registry = registry if registry is not None else load_registry()
    rc = 0
    for raw in paths:
        rel = _normalize(raw)
        if not _in_scope(rel):
            continue
        for lineno, entry, what in _scan_file(pathlib.Path(raw), module_path_for(rel), registry):
            sys.stderr.write(
                f"{rel}:{lineno}: {what} [{entry.id}] is not allowed here — "
                f"{entry.concern.strip()} Allowed modules: {', '.join(entry.allowed_modules)}\n",
            )
            rc = 1
    return rc


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Chokepoint call-site authorization checker.")
    parser.add_argument("--all", action="store_true", help="walk the whole src/teatree tree")
    parser.add_argument("paths", nargs="*", help="files to check (pre-commit passes the staged diff)")
    args = parser.parse_args(argv)
    paths = _iter_tree() if args.all else args.paths
    return check(paths)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
