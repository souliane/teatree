"""Pre-commit hook: ban raw subprocess calls in ``src/teatree/``.

Every shell-out in ``src/teatree/`` must go through ``teatree.utils.run``
(``run_checked`` / ``run_allowed_to_fail`` / ``spawn``).  Raw
``subprocess.run``, ``subprocess.Popen``, ``subprocess.check_output``,
``subprocess.check_call``, and ``subprocess.call`` are forbidden — the typed
wrapper exists to make silent failure impossible, and bypassing it reintroduces
the class of bug documented in issue #390.

Exceptions: the wrapper module itself (``src/teatree/utils/run.py``) and its
test file are allowed to import and use ``subprocess`` directly.  Type-only
references (``subprocess.Popen[str]`` as an annotation, ``subprocess.CalledProcessError``
in ``except`` clauses) are not banned — only call expressions are.
"""

import ast
import pathlib
import sys

BANNED_ATTRS: set[str] = {"run", "Popen", "check_output", "check_call", "call"}
ALLOWED_FILES: set[str] = {
    "src/teatree/utils/run.py",
    "tests/utils/test_run.py",
}
TARGET_PREFIXES: tuple[str, ...] = ("src/teatree/",)


def _is_banned_call(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if not isinstance(func.value, ast.Name):
        return None
    if func.value.id != "subprocess":
        return None
    if func.attr not in BANNED_ATTRS:
        return None
    return f"subprocess.{func.attr}(...)"


def _scan_file(path: pathlib.Path) -> list[tuple[int, str]]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        what = _is_banned_call(node)
        if what is not None:
            hits.append((node.lineno, what))
    return hits


def _normalize(path_arg: str) -> str:
    p = pathlib.Path(path_arg)
    try:
        rel = p.resolve().relative_to(pathlib.Path.cwd())
        return str(rel).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _should_check(rel: str) -> bool:
    if rel in ALLOWED_FILES:
        return False
    return any(rel.startswith(prefix) for prefix in TARGET_PREFIXES)


def main(args: list[str]) -> int:
    rc = 0
    for raw in args:
        rel = _normalize(raw)
        if not _should_check(rel):
            continue
        hits = _scan_file(pathlib.Path(raw))
        for lineno, what in hits:
            sys.stderr.write(
                f"{rel}:{lineno}: {what} is banned — "
                "use teatree.utils.run.run_checked / run_allowed_to_fail / spawn instead\n",
            )
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
