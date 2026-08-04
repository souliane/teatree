"""Self-pin the backlog-reuse precondition and its single canonical home.

The open-issue count went 12 -> 62 in a day because agents filed one issue per
FINDING instead of per root cause, and because ``gh issue list`` returns 30 rows
by default — a first-page read that reports "nothing similar is open" while a
suitable host issue sits at position 40.

``AGENTS.md`` § "Issue Creation" is the canonical home: it already owns the
filing-time preconditions the skills cite. The skills that file issues therefore
POINT at it; a second copy of the rule in a skill is the fragmentation
``skills/rules/SKILL.md`` warns about, so this module also pins that
``ac-reviewing-codebase`` no longer instructs one ticket per instance.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_AGENTS = _ROOT / "AGENTS.md"
_REVIEW_SKILL = _ROOT / "skills" / "ac-reviewing-codebase" / "SKILL.md"
_RULES_SKILL = _ROOT / "skills" / "rules" / "SKILL.md"
_SWEEP_SKILL = _ROOT / "skills" / "sweeping-tickets" / "SKILL.md"

_SECTION_HEADING_RE = re.compile(r"^##\s+Issue Creation\b")
_NEXT_SECTION_RE = re.compile(r"^##\s")


def _issue_creation_section() -> str:
    lines: list[str] = []
    in_section = False
    for line in _AGENTS.read_text(encoding="utf-8").splitlines():
        if not in_section:
            if _SECTION_HEADING_RE.match(line):
                in_section = True
            continue
        if _NEXT_SECTION_RE.match(line):
            break
        lines.append(line)
    return "\n".join(lines)


def test_the_issue_creation_section_is_found() -> None:
    assert _issue_creation_section(), "AGENTS.md has no '## Issue Creation' section to check"


def test_filing_requires_searching_the_open_backlog_first() -> None:
    section = _issue_creation_section().lower()
    assert "open backlog" in section or "open issues" in section, (
        "AGENTS.md § 'Issue Creation' must require a search of the OPEN backlog before filing"
    )


def test_the_backlog_search_names_the_pagination_default() -> None:
    section = _issue_creation_section()
    trap = (
        "`gh issue list` returns 30 rows by default, so a first-page read reports a "
        "false 'nothing open matches this'. AGENTS.md § 'Issue Creation' must name "
        "the default and the `--limit` that defeats it."
    )
    assert "30" in section, trap
    assert "--limit" in section, trap


def test_extending_an_existing_issue_is_preferred_over_a_near_duplicate() -> None:
    section = _issue_creation_section().lower()
    assert "extend" in section, (
        "AGENTS.md § 'Issue Creation' must prefer extending a suitable open issue over filing a near-duplicate"
    )


def test_granularity_is_one_issue_per_root_cause() -> None:
    section = _issue_creation_section().lower()
    assert "root cause" in section, (
        "AGENTS.md § 'Issue Creation' must state the granularity: one issue per root cause, not per finding"
    )


def test_the_reuse_rule_states_its_exception() -> None:
    section = _issue_creation_section().lower()
    assert "subsystem" in section, (
        "A rule with no stated exception is ignored wholesale the first time it does "
        "not fit: AGENTS.md § 'Issue Creation' must say that an unrelated defect in "
        "another subsystem still gets its own issue"
    )


def test_the_review_pass_files_per_root_cause_not_per_instance() -> None:
    body = _REVIEW_SKILL.read_text(encoding="utf-8")
    assert "ticket per confirmed instance" not in body, (
        "ac-reviewing-codebase's per-instance filing contradicts the backlog-reuse "
        "rule — several instances one PR would fix belong in one ticket"
    )
    assert "root cause" in body, "ac-reviewing-codebase must state the root-cause filing granularity"


def _cites_the_canonical_home(body: str) -> bool:
    """True when SOME mention of the section name sits in a filing-reuse context.

    The section name recurs across unrelated paragraphs, so keying on the first
    occurrence would pass against a pre-existing citation and guard nothing.
    """
    anchor = 'Issue Creation"'
    radius = 500
    start = 0
    while (found := body.find(anchor, start)) != -1:
        window = body[max(0, found - radius) : found + radius].lower()
        if "backlog" in window and ("reuse" in window or "extend" in window):
            return True
        start = found + 1
    return False


def test_the_issue_filing_skills_point_at_the_canonical_home() -> None:
    for path in (_RULES_SKILL, _REVIEW_SKILL, _SWEEP_SKILL):
        body = path.read_text(encoding="utf-8")
        assert _cites_the_canonical_home(body), (
            f"{path.relative_to(_ROOT)} must cite AGENTS.md § 'Issue Creation' for the "
            "backlog-reuse rule rather than restate it"
        )
