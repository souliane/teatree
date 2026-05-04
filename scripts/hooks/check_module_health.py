"""Pre-commit hook: enforce module-level architectural health.

Checks staged Python files for:
- Files exceeding a LOC threshold (default 500)
- Too many module-level functions (default 10) — prefer methods on classes
- ``dict[str, object]`` annotations — prefer typed dataclasses/TypedDict

Runs on every commit against staged files only.

See: souliane/teatree codebase audit findings
"""

import ast
import pathlib
import re
import subprocess

from commit_message import commit_message_has_relax_prefix

MAX_LOC = 500
MAX_MODULE_FUNCTIONS = 10

_DICT_OBJECT_PATTERNS = [
    "dict[str, object]",
    "Dict[str, object]",
]


def _staged_python_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "*.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [f for f in result.stdout.strip().splitlines() if f.startswith("src/")]


def _count_loc(filepath: str) -> int:
    try:
        with pathlib.Path(filepath).open(encoding="utf-8") as f:
            return sum(1 for line in f if line.strip() and not line.strip().startswith("#"))
    except OSError:
        return 0


def _count_loc_at_head(filepath: str) -> int:
    result = subprocess.run(
        ["git", "show", f"HEAD:{filepath}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0
    return sum(1 for line in result.stdout.splitlines() if line.strip() and not line.strip().startswith("#"))


def _count_module_level_functions_at_head(filepath: str) -> list[str]:
    result = subprocess.run(
        ["git", "show", f"HEAD:{filepath}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        tree = ast.parse(result.stdout)
    except SyntaxError:
        return []
    return [
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    ]


def _count_module_level_functions(filepath: str) -> list[str]:
    try:
        source = pathlib.Path(filepath).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return []

    return [
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    ]


def _added_line_numbers(filepath: str) -> set[int] | None:
    """Return the set of line numbers added/modified in the staged version, or None for new files."""
    result = subprocess.run(
        ["git", "diff", "--cached", "-U0", "--", filepath],
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout:
        return None
    added: set[int] = set()
    for match in re.finditer(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", result.stdout):
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) else 1
        added.update(range(start, start + count))
    return added


def _find_dict_object_annotations(filepath: str) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    try:
        with pathlib.Path(filepath).open(encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                stripped = line.strip()
                # Skip type alias definitions — they ARE the fix
                if stripped.startswith("type ") and "=" in stripped:
                    continue
                for pattern in _DICT_OBJECT_PATTERNS:
                    if pattern in line:
                        findings.append((line_num, line.strip()))
                        break
    except OSError:
        pass
    return findings


def main() -> int:
    files = _staged_python_files()
    if not files:
        return 0

    violations: list[str] = []

    for filepath in files:
        loc = _count_loc(filepath)
        if loc > MAX_LOC:
            prev_loc = _count_loc_at_head(filepath)
            # Only flag new violations — files already over the limit at HEAD
            # need a dedicated split-by-concern refactoring, not a commit block.
            if prev_loc <= MAX_LOC:
                violations.append(f"  {filepath}: {loc} LOC (max {MAX_LOC}). Split by concern.")

        public_functions = _count_module_level_functions(filepath)
        if len(public_functions) > MAX_MODULE_FUNCTIONS:
            prev_count = len(_count_module_level_functions_at_head(filepath))
            if prev_count <= MAX_MODULE_FUNCTIONS:
                names = ", ".join(public_functions[:5])
                violations.append(
                    f"  {filepath}: {len(public_functions)} public module-level functions "
                    f"(max {MAX_MODULE_FUNCTIONS}). Move to a class. Examples: {names}"
                )

        added_lines = _added_line_numbers(filepath)
        dict_hits = _find_dict_object_annotations(filepath)
        for line_num, _line in dict_hits:
            # Only flag lines that were added or modified in this commit
            if added_lines is None or line_num in added_lines:
                violations.append(f"  {filepath}:{line_num}: dict[str, object] — use a dataclass or TypedDict instead")

    if not violations:
        return 0

    if commit_message_has_relax_prefix():
        print(f"Module health relaxation acknowledged via relax: commit type ({len(violations)} item(s)).")
        return 0

    print("Module health violations:")
    print()
    for v in violations:
        print(v)
    print()
    print(
        "Fix these before committing. For pre-existing violations being\n"
        "refactored incrementally, use 'relax:' as the commit type.\n"
        "Example: relax(api): add dict[str, object] for legacy compat"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
