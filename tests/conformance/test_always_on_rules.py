"""The five ranked always-on rules live in one file that every agent entry point links before its first section."""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULES_FILE = _REPO_ROOT / "skills" / "rules" / "RULES.md"

_TITLES = (
    "Nothing may endanger security or leak",
    "Do not waste work, time, tokens, money or the owner's energy",
    "The owner reaches the factory only through the messaging channels",
    "Drive owner interaction toward zero",
    "A thread with the owner opens with enough context, then stays the one place for that work",
)

_ENTRY_RE = re.compile(r"^(\d+)\. \*\*(.+?):\*\* ", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#+) ", re.MULTILINE)
_SETEXT_HEADING_RE = re.compile(r"^[ \t]{0,3}\S[^\n]*\n[ \t]{0,3}(?:=+|-+)[ \t]*$", re.MULTILINE)
_NESTED_ITEM_RE = re.compile(r"^(?:[ \t]*[-*+] |[ \t]+\d+[.)] )", re.MULTILINE)
_FIRST_SECTION_RE = re.compile(r"^## ", re.MULTILINE)
_LINK_TARGET_RE = re.compile(r"\]\(([^)\s#]+)")
_FENCE_START_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _links_before_first_section(page: Path, target: Path) -> bool:
    text = re.sub(r"<!--.*?-->", "", page.read_text(encoding="utf-8"), flags=re.DOTALL)
    visible: list[str] = []
    fence = ""
    for line in text.splitlines(keepends=True):
        if fence:
            if re.fullmatch(rf"[ \t]{{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line.rstrip("\r\n")):
                fence = ""
        elif match := _FENCE_START_RE.match(line):
            fence = match.group(1)
        else:
            visible.append(line)
    head = _FIRST_SECTION_RE.split("".join(visible), maxsplit=1)[0]
    return any((page.parent / link).resolve() == target.resolve() for link in _LINK_TARGET_RE.findall(head))


def test_the_rules_are_numbered_from_zero_by_importance() -> None:
    entries = _ENTRY_RE.findall(_RULES_FILE.read_text(encoding="utf-8"))
    assert entries == [(str(rank), title) for rank, title in enumerate(_TITLES)]


@pytest.mark.parametrize(
    ("rank", "expected"),
    [
        (
            1,
            (
                "1. **Do not waste work, time, tokens, money or the owner's energy:** investigate before discarding "
                "anything (compare by content against the default branch, because squash merges hide ancestry), never "
                "repeat a check or run that was already done, take the smallest action that settles the question, and "
                "reuse existing tickets, plans and reviews. The owner's energy counts as much as any of these: never "
                "make him repeat himself, redo a step, decode shorthand or read what a link already says. "
                "Spend the owner has already configured (an API-key fallback, a budget) needs no asking; "
                "going beyond it does."
            ),
        ),
        (
            3,
            (
                "3. **Drive owner interaction toward zero:** investigate first, decide with sensible defaults, and ask "
                "only what investigation cannot settle and is truly the owner's call (money beyond what is configured, "
                "security, scope, policy, and any architectural or design decision that models cannot yet take wisely "
                "on their own: one that is costly to reverse, changes who can control, reach or override the factory, "
                "or trades safety against autonomy or cost), once, before acting, and with a recommendation. "
                "Every time the owner has to step in is a defect to remove, and the factory stays safe, "
                "self-repairing and self-improving: whatever it does by itself can be undone, never bypasses Rule 0, "
                "and is reported."
            ),
        ),
    ],
    ids=["rule-1", "rule-3"],
)
def test_owner_amendments_are_verbatim(rank: int, expected: str) -> None:
    lines = _RULES_FILE.read_text(encoding="utf-8").splitlines()
    assert next(line for line in lines if line.startswith(f"{rank}. ")) == expected


def test_the_rules_file_has_one_heading_and_no_nested_items() -> None:
    text = _RULES_FILE.read_text(encoding="utf-8")
    assert _HEADING_RE.findall(text) == ["#"]
    assert not _SETEXT_HEADING_RE.search(text)
    assert not _NESTED_ITEM_RE.findall(text)


@pytest.mark.parametrize("body", ["# Rules\n\nMore details\n------------\n", "# Rules\n\n  1) nested\n"])
def test_alternate_markdown_subheadings_and_nested_items_are_rejected(body: str) -> None:
    assert _HEADING_RE.findall(body) != ["#"] or _SETEXT_HEADING_RE.search(body) or _NESTED_ITEM_RE.findall(body)


@pytest.mark.parametrize("page", ["AGENTS.md", "CLAUDE.md", "skills/rules/SKILL.md"])
def test_an_entry_point_links_the_rules_before_its_first_section(page: str) -> None:
    assert _links_before_first_section(_REPO_ROOT / page, _RULES_FILE)


@pytest.mark.parametrize(
    ("body", "linked"),
    [
        ("# Page\n\nRead [`rules/RULES.md`](rules/RULES.md).\n\n## Section\n", True),
        ("# Page\n\n## Section\n\nRead [the rules](rules/RULES.md).\n", False),
        ("# Page\n\nRead [the rules](rules/OTHER.md).\n\n## Section\n", False),
        ("# Page\n\n<!-- Read [the rules](rules/RULES.md). -->\n\n## Section\n", False),
        ("# Page\n\n```md\nRead [the rules](rules/RULES.md).\n```\n\n## Section\n", False),
        ("# Page\n\n```md\n```md\nRead [the rules](rules/RULES.md).\n```\n\n## Section\n", False),
    ],
    ids=[
        "linked-in-preamble",
        "linked-after-first-section",
        "linked-to-another-file",
        "commented-link",
        "fenced-link",
        "info-string-is-not-a-closer",
    ],
)
def test_a_planted_page_is_judged_by_where_and_what_it_links(tmp_path: Path, body: str, *, linked: bool) -> None:
    (tmp_path / "rules").mkdir()
    target = tmp_path / "rules" / "RULES.md"
    target.write_text("# Rules\n", encoding="utf-8")
    page = tmp_path / "PAGE.md"
    page.write_text(body, encoding="utf-8")
    assert _links_before_first_section(page, target) is linked
