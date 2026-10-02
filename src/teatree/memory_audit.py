"""Scan Claude memory files for entries that should be promoted to skills."""

import re
from dataclasses import dataclass
from pathlib import Path

_GUARDRAIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bNEVER\b", re.IGNORECASE),
    re.compile(r"\bALWAYS\b", re.IGNORECASE),
    re.compile(r"\bMUST\b", re.IGNORECASE),
    re.compile(r"\bdo NOT\b", re.IGNORECASE),
    re.compile(r"\bbefore .{3,30} (do|run|check)\b", re.IGNORECASE),
    re.compile(r"\bafter .{3,30} (do|run|check)\b", re.IGNORECASE),
    re.compile(r"\bwhen .{3,30} (always|never|must)\b", re.IGNORECASE),
    re.compile(r"\bnon-negotiable\b", re.IGNORECASE),
)

_SKILL_HINT_MAP: dict[str, str] = {
    "commit": "ship",
    "push": "ship",
    "mr": "ship",
    "pr": "ship",
    "merge": "ship",
    "review": "review",
    "test": "test",
    "e2e": "e2e",
    "playwright": "e2e",
    "debug": "debug",
    "worktree": "workspace",
    "workspace": "workspace",
    "db": "workspace",
    "docker": "workspace",
    "retro": "retro",
    "ticket": "ticket",
    "skill": "rules",
    "import": "code",
    "lint": "code",
    "ruff": "code",
    "type": "code",
}


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    path: Path
    name: str
    entry_type: str
    body: str
    matched_patterns: tuple[str, ...]
    suggested_skill: str


def discover_memory_dirs() -> list[Path]:
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.is_dir():
        return []
    return sorted(d for project in claude_dir.iterdir() if project.is_dir() for d in [project / "memory"] if d.is_dir())


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("---", 3)
    if end == -1:
        return {}, text
    frontmatter_text = text[3:end].strip()
    body = text[end + 3 :].strip()
    fields: dict[str, str] = {}
    for line in frontmatter_text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields, body


def _detect_guardrail_patterns(body: str) -> tuple[str, ...]:
    return tuple(pattern.pattern for pattern in _GUARDRAIL_PATTERNS if pattern.search(body))


def _suggest_skill(body: str) -> str:
    lowered = body.lower()
    for keyword, skill in _SKILL_HINT_MAP.items():
        if keyword in lowered:
            return skill
    return "rules"


def scan_memory_dir(memory_dir: Path) -> list[MemoryEntry]:
    entries: list[MemoryEntry] = []
    for md_file in sorted(memory_dir.glob("*.md")):
        if md_file.name == "MEMORY.md":
            continue
        text = md_file.read_text(encoding="utf-8")
        fields, body = _parse_frontmatter(text)
        entry_type = fields.get("type", fields.get("metadata", {}) if isinstance(fields.get("metadata"), dict) else "")
        if isinstance(entry_type, dict):
            entry_type = entry_type.get("type", "")
        matched = _detect_guardrail_patterns(body)
        if not matched:
            continue
        entries.append(
            MemoryEntry(
                path=md_file,
                name=fields.get("name", md_file.stem),
                entry_type=str(entry_type),
                body=body,
                matched_patterns=matched,
                suggested_skill=_suggest_skill(body),
            )
        )
    return entries


def scan_all() -> list[MemoryEntry]:
    results: list[MemoryEntry] = []
    for memory_dir in discover_memory_dirs():
        results.extend(scan_memory_dir(memory_dir))
    return results
