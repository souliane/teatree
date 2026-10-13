#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# requires-python = ">=3.12"
# ///
"""Auto-update the skills catalogue in README.md from SKILL.md frontmatter."""

import re
import sys
from pathlib import Path

from teatree.core.diagrams import inject_between_markers
from teatree.loops.seed import DEFAULT_LOOPS

ROOT_DIR = Path(__file__).resolve().parents[2]
README_PATH = ROOT_DIR / "README.md"

BEGIN = "<!-- BEGIN SKILLS -->"
END = "<!-- END SKILLS -->"
LOOPS_BEGIN = "<!-- BEGIN LOOPS -->"
LOOPS_END = "<!-- END LOOPS -->"
FRONTMATTER_RE = re.compile(r"^---\s*\n(.+?)\n---", re.DOTALL)


def _parse_frontmatter(path: Path) -> dict[str, str]:
    """Extract YAML-ish key: value pairs from SKILL.md frontmatter."""
    m = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def _build_table() -> str:
    skills: list[tuple[str, str]] = []  # (name, phase/description)

    for skill_md in sorted(ROOT_DIR.glob("skills/*/SKILL.md")):
        meta = _parse_frontmatter(skill_md)
        name = meta.get("name", skill_md.parent.name)
        desc = meta.get("description", "")
        # Use only the short part before "Use when" trigger words
        phase = desc.split(". Use when")[0].split(". Use this")[0] if desc else ""
        skills.append((name, phase))

    lines = [
        "| Skill | Phase |",
        "|-------|-------|",
    ]
    for name, phase in skills:
        lines.append(f"| `{name}` | {phase} |")
    return "\n".join(lines)


def _build_loops_table() -> str:
    rows = ["| Loop | Purpose |", "|------|---------|"]
    rows.extend(f"| `{spec.name}` | {spec.description.split('. ', 1)[0].rstrip('.')} |" for spec in DEFAULT_LOOPS)
    return "\n".join(rows)


def main() -> int:
    if not README_PATH.exists():
        print(f"Error: {README_PATH} not found", file=sys.stderr)
        return 1

    text = README_PATH.read_text(encoding="utf-8")

    try:
        new_text = inject_between_markers(text, begin=BEGIN, end=END, block=_build_table())
        new_text = inject_between_markers(new_text, begin=LOOPS_BEGIN, end=LOOPS_END, block=_build_loops_table())
    except ValueError as exc:
        print(f"Error: README.md {exc}", file=sys.stderr)
        return 1

    if text == new_text:
        return 0

    README_PATH.write_text(new_text, encoding="utf-8")
    print("Updated README.md skills catalogue and loop inventory")
    return 1  # signal pre-commit that file was modified


if __name__ == "__main__":
    sys.exit(main())
