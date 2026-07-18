"""Pre-commit hook: a broad-except handler must be observable, never swallow.

A handler catching ``Exception`` / ``BaseException`` / bare ``except:`` whose
body neither logs (``logger.*`` / ``log.*`` / ``logging.*``), surfaces the cause
(``print`` / ``*.echo``), nor re-raises is a fail-open seam: a real failure is
flattened into a sentinel indistinguishable from success. The hook flags it. It
also flags a broad handler that returns a gate **success sentinel**
(``return True``) — the schema_guard fail-open class, where an unexpected error
reports the check passed.

Genuine tick-safety-net / resolve-or-skip seams that intentionally degrade to a
neutral value declare an entry in ``src/teatree/quality/broad_except_optout.yaml``
so the exemption is reviewed in one place instead of scattered per-file suppressions.

Staged warn-first (manual stage): the existing legit broad handlers must not
block commits before the class is fully migrated. Sibling of
``check_no_silent_skip.py`` and ``check_module_health.py``.
"""

import ast
from pathlib import Path

import yaml

from teatree.utils.run import run_allowed_to_fail

_OPTOUT_REGISTRY = Path("src/teatree/quality/broad_except_optout.yaml")

_BROAD_NAMES = frozenset({"Exception", "BaseException"})
_LOG_RECEIVERS = frozenset({"logger", "log", "logging"})
_SURFACING_CALLS = frozenset({"print", "echo"})
_SUCCESS_SENTINELS = frozenset({True})


def _staged_python_files() -> list[str]:
    result = run_allowed_to_fail(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "*.py"],
        expected_codes=None,
    )
    return [f for f in result.stdout.strip().splitlines() if f.startswith("src/teatree/")]


def load_optouts(registry: Path) -> set[str]:
    if not registry.exists():
        return set()
    data = yaml.safe_load(registry.read_text(encoding="utf-8")) or {}
    return {entry["file"] for entry in data.get("optouts", []) if "file" in entry}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    exc = handler.type
    if exc is None:
        return True  # bare ``except:``
    if isinstance(exc, ast.Name):
        return exc.id in _BROAD_NAMES
    if isinstance(exc, ast.Tuple):
        return any(isinstance(el, ast.Name) and el.id in _BROAD_NAMES for el in exc.elts)
    return False


def _logs_or_surfaces(body: list[ast.stmt]) -> bool:
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name) and func.value.id in _LOG_RECEIVERS:
                return True
            if func.attr in _SURFACING_CALLS:
                return True
        elif isinstance(func, ast.Name) and func.id in _SURFACING_CALLS:
            return True
    return False


def _reraises(body: list[ast.stmt]) -> bool:
    return any(isinstance(node, ast.Raise) for node in ast.walk(ast.Module(body=body, type_ignores=[])))


def _returns_success_sentinel(body: list[ast.stmt]) -> bool:
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if (
            isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and node.value.value in _SUCCESS_SENTINELS
            and isinstance(node.value.value, bool)
        ):
            return True
    return False


def _violations_in_file(filepath: str, source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node):
            continue
        if _logs_or_surfaces(node.body) or _reraises(node.body):
            if not _returns_success_sentinel(node.body):
                continue
            findings.append(
                f"  {filepath}:{node.lineno}: broad except returns a success sentinel — "
                f"an unexpected error reports the check passed (fail-open)."
            )
            continue
        findings.append(
            f"  {filepath}:{node.lineno}: broad except swallows with no log and no raise. "
            f"Log the cause (logger.*) or re-raise, or declare an opt-out."
        )
    return findings


def _staged_source(filepath: str) -> str:
    result = run_allowed_to_fail(
        ["git", "show", f":{filepath}"],
        expected_codes=None,
    )
    if result.returncode == 0:
        return result.stdout
    return Path(filepath).read_text(encoding="utf-8") if Path(filepath).exists() else ""


def main() -> int:
    optouts = load_optouts(_OPTOUT_REGISTRY)
    violations: list[str] = []
    for filepath in _staged_python_files():
        if filepath in optouts:
            continue
        source = _staged_source(filepath)
        if source:
            violations.extend(_violations_in_file(filepath, source))

    if not violations:
        return 0

    print("Broad-except observability violations:")
    print()
    for v in violations:
        print(v)
    print()
    print(
        "A broad except must log the cause or re-raise — never swallow a failure\n"
        "into a sentinel, and never report a gate's success on an unexpected error.\n"
        "If a seam genuinely degrades to a neutral value (tick safety net,\n"
        "resolve-or-skip), declare it in src/teatree/quality/broad_except_optout.yaml."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
