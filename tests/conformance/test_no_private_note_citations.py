"""Public source and documentation state rules without private note-file names."""

import re
from pathlib import Path

from tests.conformance._src_tree import REPO_ROOT

_ROOTS = ("src", "skills", "docs", "hooks", "BLUEPRINT.md")
_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".txt", ".json", ".sh"}
_NOTE_CITATION = re.compile(r"(?<![A-Za-z0-9_])feedback_(?!x(?:_y)?\b)[a-z0-9][a-z0-9_]*")


def private_note_citations(root: Path) -> list[str]:
    hits: list[str] = []
    for entry in _ROOTS:
        path = root / entry
        if not path.exists():
            raise FileNotFoundError(path)
        files = (
            [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in _SUFFIXES)
        )
        for file in files:
            for number, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if _NOTE_CITATION.search(line):
                    hits.append(f"{file.relative_to(root)}:{number}")
    return hits


def test_public_source_has_no_private_note_file_citations() -> None:
    assert private_note_citations(REPO_ROOT) == []


def test_scan_finds_each_root_and_spares_generic_placeholders(tmp_path: Path) -> None:
    expected: list[str] = []
    for entry in _ROOTS:
        path = tmp_path / entry
        if path.suffix:
            path.write_text("feedback_example_rule\n", encoding="utf-8")
        else:
            path.mkdir()
            path /= "example.md"
            path.write_text("feedback_x.md feedback_x_y.md\nfeedback_example_rule\n", encoding="utf-8")
        expected.append(f"{path.relative_to(tmp_path)}:{1 if entry == 'BLUEPRINT.md' else 2}")

    assert sorted(private_note_citations(tmp_path)) == sorted(expected)
