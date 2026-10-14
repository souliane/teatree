# test-path: cross-cutting — pins BLUEPRINT.md's appendix roster against the docs/blueprint/ tree.
"""BLUEPRINT's appendix table claims to list every file in docs/blueprint/, so it must."""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ROSTER_ROW = re.compile(r"^\| \[[^]]+\]\(docs/blueprint/([^)]+\.md)\) \|", re.MULTILINE)


def _roster() -> set[str]:
    text = (_ROOT / "BLUEPRINT.md").read_text(encoding="utf-8")
    section = text[text.index("## Architectural Appendices") :]
    return set(_ROSTER_ROW.findall(section))


def test_every_appendix_is_on_the_roster() -> None:
    on_disk = {path.name for path in (_ROOT / "docs" / "blueprint").glob("*.md")}

    assert sorted(on_disk - _roster()) == []


def test_every_roster_row_names_an_appendix_on_disk() -> None:
    assert sorted(name for name in _roster() if not (_ROOT / "docs" / "blueprint" / name).is_file()) == []
