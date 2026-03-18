"""Ensure every SKILL.md version matches the project version in pyproject.toml."""

import re
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = ROOT_DIR / "pyproject.toml"
VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
FM_VERSION_RE = re.compile(r"^\s*version:\s*(.+)$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.+?)\n---", re.DOTALL)


def _project_version() -> str | None:
    """Read the project version from pyproject.toml."""
    if not PYPROJECT_PATH.exists():
        return None
    m = VERSION_RE.search(PYPROJECT_PATH.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _skill_version(path: Path) -> str | None:
    """Read the version from a SKILL.md frontmatter."""
    text = path.read_text(encoding="utf-8")
    fm = FRONTMATTER_RE.match(text)
    if not fm:
        return None
    m = FM_VERSION_RE.search(fm.group(1))
    return m.group(1).strip().strip("\"'") if m else None


def _fix_version(path: Path, expected: str) -> bool:
    """Rewrite the version in a SKILL.md frontmatter. Returns True if modified."""
    text = path.read_text(encoding="utf-8")
    new_text = FM_VERSION_RE.sub(f"  version: {expected}", text, count=1)
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def main() -> int:
    expected = _project_version()
    if not expected:
        print("Error: could not read version from pyproject.toml", file=sys.stderr)
        return 1

    issues = 0
    for skill_md in sorted(ROOT_DIR.glob("t3-*/SKILL.md")):
        actual = _skill_version(skill_md)
        if actual != expected:
            rel = skill_md.relative_to(ROOT_DIR)
            if _fix_version(skill_md, expected):
                print(f"{rel}: fixed version {actual!r} -> {expected!r}")
            else:
                print(f"{rel}: version {actual!r} != expected {expected!r} (could not auto-fix)", file=sys.stderr)
            issues += 1

    if issues:
        print(f"{issues} skill(s) had wrong versions")
    return 1 if issues else 0
