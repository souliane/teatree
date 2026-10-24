"""The message-count history trim is gone; no source, doc, skill or test may still name it."""

import re
from pathlib import Path

from tests.conformance._src_tree import REPO_ROOT

_REMOVED = re.compile(r"\b(?:compact_history|pin_head|agent_compaction_keep_recent)\b")
_ROOTS = ("src", "tests", "docs", "skills", "BLUEPRINT.md", "README.md", "AGENTS.md")
_RETIRED_KEY_LINE = "RETIRED agent_compaction_keep_recent"
_THIS_FILE = Path(__file__).resolve()
_SUFFIXES = {".py", ".md", ".txt", ".yaml", ".yml", ".toml"}


def _scanned_files(root: Path, entries: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for entry in entries:
        path = root / entry
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in sorted(path.rglob("*")) if p.is_file() and p.suffix in _SUFFIXES)
    return [p for p in files if p.resolve() != _THIS_FILE]


def stale_references(root: Path, entries: tuple[str, ...] = _ROOTS) -> list[str]:
    hits: list[str] = []
    for path in _scanned_files(root, entries):
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if _REMOVED.search(line) and line.strip() != _RETIRED_KEY_LINE:
                hits.append(f"{path.relative_to(root)}:{number}: {line.strip()[:100]}")
    return hits


def test_no_source_doc_or_skill_names_the_removed_message_count_trim() -> None:
    assert stale_references(REPO_ROOT) == []


def test_the_scan_finds_a_planted_reference_and_spares_the_retired_key_marker(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "kept.txt").write_text(f"{_RETIRED_KEY_LINE}\n", encoding="utf-8")
    (tmp_path / "src" / "leak.py").write_text("compact_history(messages)\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("Set `agent_compaction_keep_recent` to trim.\n", encoding="utf-8")

    assert sorted(stale_references(tmp_path)) == [
        "README.md:1: Set `agent_compaction_keep_recent` to trim.",
        "src/leak.py:1: compact_history(messages)",
    ]
