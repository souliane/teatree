"""Manual-stage hook: scan the staged diff for greppable anti-patterns.

Loads the catalog (``teatree.quality.catalog``), filters ``detection ==
greppable``, and runs each entry's ``grep_hint`` over the ADDED lines of the
staged diff. Reports a finding per match: the entry, the file:line, and the
preferred pattern.

Registered ``stages: [manual]`` ONLY — it is informational, NOT a commit gate.
A grep_hint can false-positive, and a false-positive COMMIT gate could lock the
factory out; promotion to a blocking gate is a deliberate, deferred follow-up.

The catalog's OWN source files are excluded from the scan: the YAML literally
contains every grep_hint, and the generated doc / loader / tests echo them, so
scanning them would self-trigger on every catalog edit.

See: souliane/teatree#166
"""

import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SELF_EXCLUDE_PREFIXES: tuple[str, ...] = (
    "src/teatree/quality/",
    "docs/generated/antipattern-catalog.md",
    "tests/quality/",
    "scripts/hooks/check_antipatterns.py",
    "scripts/hooks/generate_antipattern_catalog.py",
    "scripts/hooks/check_antipattern_catalog_sync.py",
)


def _staged_diff() -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "--diff-filter=ACMR", "-U0"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def _added_lines(diff: str) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    current_file = ""
    line_num = 0
    for raw_line in diff.splitlines():
        if raw_line.startswith("+++ "):
            current_file = raw_line[4:].removeprefix("b/")
        elif raw_line.startswith("@@ "):
            for part in raw_line.split():
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


def _is_excluded(path: str) -> bool:
    return path.startswith(_SELF_EXCLUDE_PREFIXES)


def main() -> int:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
    from teatree.quality.catalog import load_catalog

    compiled = [(e, re.compile(e.grep_hint)) for e in load_catalog() if e.grep_hint is not None]

    diff = _staged_diff()
    if not diff:
        return 0

    findings: list[str] = []
    for filename, line_num, line in _added_lines(diff):
        if _is_excluded(filename) or not filename.endswith((".py", ".yaml", ".yml")):
            continue
        for entry, pattern in compiled:
            if pattern.search(line):
                findings.append(f"  {filename}:{line_num}: [{entry.id}] {line.strip()[:100]}")

    if not findings:
        print("check_antipatterns: no greppable anti-patterns in the staged diff.")
        return 0

    print("Greppable anti-patterns found in the staged diff (informational):")
    print()
    for f in findings:
        print(f)
    print()
    print("See docs/generated/antipattern-catalog.md for the preferred pattern.")
    print("This is a MANUAL-stage check; it does not block the commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
