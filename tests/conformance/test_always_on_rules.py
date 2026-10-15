"""The five ranked always-on rules live in one file that every agent entry point links before its first section."""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULES_FILE = _REPO_ROOT / "skills" / "rules" / "RULES.md"

_TITLES = (
    "Nothing may endanger security or leak",
    "Do not waste work, time, tokens or money",
    "The owner reaches the factory only through the messaging channels",
    "Drive owner interaction toward zero",
    "A thread with the owner opens with enough context, then stays the one place for that work",
)

_ENTRY_RE = re.compile(r"^(\d+)\. \*\*(.+?):\*\* ", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#+) ", re.MULTILINE)
_NESTED_ITEM_RE = re.compile(r"^(?:[ \t]*[-*+] |[ \t]+\d+\. )", re.MULTILINE)
_FIRST_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
_LINK_TARGET_RE = re.compile(r"\]\(([^)\s#]+)")


def _links_before_first_section(page: Path, target: Path) -> bool:
    head = _FIRST_SECTION_RE.split(page.read_text(encoding="utf-8"), maxsplit=1)[0]
    return any((page.parent / link).resolve() == target.resolve() for link in _LINK_TARGET_RE.findall(head))


def test_the_rules_are_numbered_from_zero_by_importance() -> None:
    entries = _ENTRY_RE.findall(_RULES_FILE.read_text(encoding="utf-8"))
    assert entries == [(str(rank), title) for rank, title in enumerate(_TITLES)]


def test_the_rules_file_has_one_heading_and_no_nested_items() -> None:
    text = _RULES_FILE.read_text(encoding="utf-8")
    assert _HEADING_RE.findall(text) == ["#"]
    assert not _NESTED_ITEM_RE.findall(text)


@pytest.mark.parametrize("page", ["AGENTS.md", "CLAUDE.md", "skills/rules/SKILL.md"])
def test_an_entry_point_links_the_rules_before_its_first_section(page: str) -> None:
    assert _links_before_first_section(_REPO_ROOT / page, _RULES_FILE)


@pytest.mark.parametrize(
    ("body", "linked"),
    [
        ("# Page\n\nRead [`rules/RULES.md`](rules/RULES.md).\n\n## Section\n", True),
        ("# Page\n\n## Section\n\nRead [the rules](rules/RULES.md).\n", False),
        ("# Page\n\nRead [the rules](rules/OTHER.md).\n\n## Section\n", False),
    ],
    ids=["linked-in-preamble", "linked-after-first-section", "linked-to-another-file"],
)
def test_a_planted_page_is_judged_by_where_and_what_it_links(tmp_path: Path, body: str, *, linked: bool) -> None:
    (tmp_path / "rules").mkdir()
    target = tmp_path / "rules" / "RULES.md"
    target.write_text("# Rules\n", encoding="utf-8")
    page = tmp_path / "PAGE.md"
    page.write_text(body, encoding="utf-8")
    assert _links_before_first_section(page, target) is linked
