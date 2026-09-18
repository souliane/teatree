"""The engagement skill's filing block must carry its moderation counterweight.

souliane/teatree#4762: the block told the session to FILE and said nothing about
checking first, so one session filed 13 issues having run ``gh issue list`` zero
times — roughly a third were duplicates, enforcement-halves of a sibling, or
already delivered. ``AGENTS.md`` § "Issue Creation" already held search-first /
extend / one-root-cause; none of it was reachable from the skill a session
actually reads.

Both halves fail the same way — a dropped request and an inflated backlog are
each invisible to the operator — so neither is safe to state alone. A prose
counterweight rots the moment someone tightens the filing instruction without
carrying it, and the ordering (search BEFORE the create call is reachable) is
what a session reading top-to-bottom actually obeys. Pinned here rather than
trusted.
"""

from pathlib import Path

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "interactive" / "SKILL.md"

FILING_HEADING = "## This session does not implement"
FILE_IT_INSTRUCTION = "**File the ticket.**"
CREATE_CALL = "mcp__teatree__github_issue_create"
SEARCH_TOOL = "mcp__teatree__github_issue_search"
SEARCH_COMMAND = "gh issue list"
PAGE_SIZE_FLAG = "--limit 200"


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _filing_section(text: str) -> str:
    """The filing section's body, from its heading up to the next ``## ``."""
    return text.partition(FILING_HEADING)[2].partition("\n## ")[0]


def _occurrences(text: str, token: str) -> list[int]:
    found, start = [], text.find(token)
    while start != -1:
        found.append(start)
        start = text.find(token, start + 1)
    return found


def test_filing_section_still_carries_the_file_it_instruction() -> None:
    section = _filing_section(_skill_text())
    assert FILE_IT_INSTRUCTION in section, (
        f"{FILING_HEADING!r} no longer carries {FILE_IT_INSTRUCTION!r} — the anchor every other "
        "assertion here hangs off has moved, so they are passing vacuously. Re-anchor them."
    )


def test_search_command_names_the_page_size() -> None:
    lines = [line for line in _filing_section(_skill_text()).splitlines() if SEARCH_COMMAND in line]
    assert lines, f"the filing section names no {SEARCH_COMMAND!r} — nothing tells the session to check first"
    assert any(PAGE_SIZE_FLAG in line for line in lines), (
        f"no {SEARCH_COMMAND!r} line passes {PAGE_SIZE_FLAG!r}: {lines}. The default page size is 30, so a "
        "flagless search reports 'nothing open matches' while the host issue sits below the fold."
    )


def test_filing_section_carries_extend_and_one_root_cause() -> None:
    section = _filing_section(_skill_text()).lower()
    missing = [token for token in ("extend", "root cause") if token not in section]
    assert not missing, (
        f"the filing section is missing {missing} — searching without them yields a session that finds "
        "the host issue and files a near-duplicate beside it anyway."
    )


def test_the_mcp_read_is_the_named_search_surface() -> None:
    section = _filing_section(_skill_text())
    assert SEARCH_TOOL in section, (
        f"the filing section names {SEARCH_COMMAND!r} but not {SEARCH_TOOL!r} — the create call one paragraph "
        "below is an MCP tool, and the MCP read pages at 100 where the CLI default stops at 30."
    )


def test_every_create_call_is_preceded_by_both_search_surfaces() -> None:
    text = _skill_text()
    create_calls = _occurrences(text, CREATE_CALL)
    assert create_calls, f"no {CREATE_CALL!r} in the skill — this guard's subject is gone, so it proves nothing"
    for surface in (SEARCH_TOOL, SEARCH_COMMAND):
        found = _occurrences(text, surface)
        assert found, f"no {surface!r} anywhere in the skill"
        unguarded = [offset for offset in create_calls if offset < found[0]]
        assert not unguarded, (
            f"{CREATE_CALL!r} is reachable at {unguarded} before the first {surface!r} at {found[0]} — "
            "a session reading top-to-bottom hits 'create an issue' having never been told to check the backlog."
        )
