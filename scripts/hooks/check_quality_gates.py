"""Pre-commit hook: detect quality gate relaxations in staged changes.

Flags additions to lint ignore lists, coverage omit patterns, new ``# noqa`` /
``# pragma: no cover`` annotations, and ``fail_under`` decreases.  Exits non-zero
when relaxations are found so the author must acknowledge them explicitly.

See: souliane/teatree#17
"""

import re
import subprocess

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
        violations.extend(
            f"  {filename}:{line_num}: {line.strip()}"
            for filename, line_num, line in _added_lines(code_diff)
            if filename != "pyproject.toml"
            and not filename.startswith("tests/")
            and not filename.startswith("scripts/hooks/")
            and not filename.startswith("e2e/")
            for pattern in _CODE_RELAXATION_PATTERNS
            if pattern in line
        )

    if violations:
        print("Quality gate relaxation detected:")
        print()
        for v in violations:
            print(v)
        print()
        print(
            "If intentional, add 'relax:' to your commit message explaining why.\n"
            "Example: relax: add S404 to test ignores — subprocess in test fixtures is trusted"
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
