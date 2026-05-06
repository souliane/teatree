"""Commit-msg hook: detect quality gate relaxations in staged changes.

Flags additions to lint ignore lists, coverage omit patterns, new ``# noqa`` /
``# pragma: no cover`` annotations, and ``fail_under`` decreases. Exits non-zero
when relaxations are found — there is no bypass. Refactor instead.

Suppressions that are renamed-in-place (same marker added and removed within
the same file) net to zero and are not flagged.

See: souliane/teatree#17
"""

import re
import subprocess
from collections import Counter

# Patterns in pyproject.toml that indicate structural config relaxation.
_PYPROJECT_KEYWORD_PATTERNS: list[str] = [
    "per-file-ignores",
    "lint.ignore",
    "lint.unfixable",
    "fail_under",
    "omit",
    "--no-cov",
    "--no-verify",
]

# Inline suppressions in source files.
_CODE_RELAXATION_PATTERNS: list[str] = [
    "# noqa",
    "# type: ignore",
    "# pragma: no cover",
]

# A line like '  "S603",' or '  "E501",' — a ruff rule code being added to a list.
_RULE_CODE_RE = re.compile(r'^\s*"[A-Z]+\d+[A-Z]?\d*"')


def _staged_diff(path_filter: str = "") -> str:
    cmd = ["git", "diff", "--cached", "--diff-filter=ACMR", "-U0"]
    if path_filter:
        cmd.extend(["--", path_filter])
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout


def _added_lines(diff: str) -> list[tuple[str, int, str]]:
    """Extract added lines from unified diff output.

    Returns list of (filename, line_number, line_text) tuples.
    """
    findings: list[tuple[str, int, str]] = []
    current_file = ""
    line_num = 0

    for raw_line in diff.splitlines():
        if raw_line.startswith("+++ "):
            # Handle both "+++ b/path" (default) and "+++ path" (no-prefix) formats
            path = raw_line[4:]
            current_file = path.removeprefix("b/")
        elif raw_line.startswith("@@ "):
            parts = raw_line.split()
            for part in parts:
                if part.startswith("+") and "," in part:
                    line_num = int(part.split(",")[0][1:])
                    break
                if part.startswith("+") and part[1:].isdigit():
                    line_num = int(part[1:])
                    break
        elif raw_line.startswith("+") and not raw_line.startswith("+++"):
            findings.append((current_file, line_num, raw_line[1:]))
            line_num += 1

    return findings


def _removed_lines(diff: str) -> list[tuple[str, str]]:
    """Extract removed lines from unified diff output.

    Returns list of (filename, line_text) tuples. The filename is the new
    (post-rename) path so callers can compare against added lines in the
    same file scope.
    """
    findings: list[tuple[str, str]] = []
    current_file = ""

    for raw_line in diff.splitlines():
        if raw_line.startswith("+++ "):
            path = raw_line[4:]
            current_file = path.removeprefix("b/")
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            findings.append((current_file, raw_line[1:]))

    return findings


def _suppression_marker(line: str) -> str | None:
    """Return the suppression marker (with rule code) found in `line`, or None."""
    for pattern in _CODE_RELAXATION_PATTERNS:
        idx = line.find(pattern)
        if idx >= 0:
            return line[idx:].rstrip()
    return None


def _is_pyproject_relaxation(line: str) -> bool:
    """Return True if the added line looks like a quality gate relaxation."""
    lower = line.lower()
    for pattern in _PYPROJECT_KEYWORD_PATTERNS:
        if pattern.lower() in lower:
            return True
    return bool(_RULE_CODE_RE.match(line))


def main() -> int:
    violations: list[str] = []

    pyproject_diff = _staged_diff("pyproject.toml")
    if pyproject_diff:
        for filename, line_num, line in _added_lines(pyproject_diff):
            if _is_pyproject_relaxation(line):
                violations.append(f"  {filename}:{line_num}: {line.strip()}")

    code_diff = _staged_diff()
    if code_diff:
        # Renamed-in-place suppressions (same marker removed and re-added in
        # the same file) cancel out. Build a counter of removed markers per
        # file, then decrement as we encounter matching adds.
        removed_markers: Counter[tuple[str, str]] = Counter()
        for filename, line in _removed_lines(code_diff):
            marker = _suppression_marker(line)
            if marker is not None:
                removed_markers[filename, marker] += 1

        skip_prefixes = ("tests/", "scripts/hooks/", "e2e/", "skills/", "docs/")
        for filename, line_num, line in _added_lines(code_diff):
            if filename == "pyproject.toml" or filename.startswith(skip_prefixes):
                continue
            marker = _suppression_marker(line)
            if marker is None:
                continue
            key = (filename, marker)
            if removed_markers[key] > 0:
                removed_markers[key] -= 1
                continue
            violations.append(f"  {filename}:{line_num}: {line.strip()}")

    if not violations:
        return 0

    print("Quality gate relaxation detected:")
    print()
    for v in violations:
        print(v)
    print()
    print(
        "Remove the suppression and fix the underlying issue. If the\n"
        "suppression is genuinely required (e.g., trusted subprocess in a\n"
        "test fixture), move the affected code into a directory the hook\n"
        "already exempts (tests/, scripts/hooks/, e2e/, skills/, docs/)."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
