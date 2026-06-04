"""Pre-commit hook: ban silently-disabled tests.

A test disabled with no runtime condition is dead coverage that looks alive —
the suite stays green while the behaviour it pinned drifts. Flags, in staged
test files:

- ``@pytest.mark.skip`` / ``@unittest.skip`` (unconditional skip)
- ``@pytest.mark.skipif(True)`` / ``skipIf(1, ...)`` — a literal-truthy condition

Conditional skips keyed on the environment (``skipif(shutil.which(...) is None)``,
``skipif(not MARKITDOWN_INSTALLED)``) are legitimate and pass — the test runs
wherever its prerequisite exists. ``xfail`` is allowed: it still runs and
asserts the failure shape. There is no bypass — delete the test or fix it.

Mirrors openclaw's ``vitest/no-disabled-tests``. AST-based, sibling of
``check_module_health.py``.
"""

import ast
import subprocess

_UNCONDITIONAL_SKIP_NAMES = frozenset({"skip"})
_SKIPIF_NAMES = frozenset({"skipif", "skipIf"})


def _staged_test_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "*.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [f for f in result.stdout.strip().splitlines() if f.startswith(("tests/", "e2e/"))]


def _decorator_tail(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _decorator_tail(node.func)
    return ""


def _is_always_truthy(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and isinstance(node.operand, ast.Constant):
        return not bool(node.operand.value)
    return False


def _is_unconditional_skip(decorator: ast.expr) -> bool:
    tail = _decorator_tail(decorator)
    if tail in _UNCONDITIONAL_SKIP_NAMES:
        return True
    if tail in _SKIPIF_NAMES and isinstance(decorator, ast.Call):
        return bool(decorator.args) and _is_always_truthy(decorator.args[0])
    return False


def _violations_in_file(filepath: str, source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            if _is_unconditional_skip(decorator):
                tail = _decorator_tail(decorator)
                findings.append(
                    f"  {filepath}:{decorator.lineno}: {node.name} disabled via {tail} (unconditional skip)"
                )
    return findings


def _staged_source(filepath: str) -> str:
    result = subprocess.run(
        ["git", "show", f":{filepath}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def main() -> int:
    violations: list[str] = []
    for filepath in _staged_test_files():
        source = _staged_source(filepath)
        if source:
            violations.extend(_violations_in_file(filepath, source))

    if not violations:
        return 0

    print("Silently-disabled tests detected:")
    print()
    for v in violations:
        print(v)
    print()
    print(
        "An unconditional skip is dead coverage. Delete the test or fix it.\n"
        "A genuine environment dependency uses a conditional skipif\n"
        "(e.g. skipif(shutil.which('git') is None)) — those run wherever the\n"
        "prerequisite exists and are allowed. There is no bypass."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
