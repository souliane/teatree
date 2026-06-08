"""Section-scoped system-prompt extraction — the eval token-cost lever."""

import pytest

from teatree.eval.context_budget import MissingSectionError, extract_sections

_SKILL = """---
name: rules
---

# Agent Rules

Preamble paragraph that frames the rules.

## Background Long Operations

Background anything over 15 seconds. Use a Task or run_in_background.

## Always Use AskUserQuestion for Questions

Never ask inline. Use the structured tool.

## Worktree-First Work

All work happens in a worktree.
"""


class TestExtractSections:
    def test_extracts_a_single_named_section_verbatim(self) -> None:
        out = extract_sections(_SKILL, ("Background Long Operations",))
        assert "Background anything over 15 seconds" in out
        assert "## Background Long Operations" in out

    def test_drops_the_unrelated_sections(self) -> None:
        out = extract_sections(_SKILL, ("Background Long Operations",))
        assert "AskUserQuestion" not in out
        assert "Worktree-First Work" not in out

    def test_keeps_the_preamble_for_framing(self) -> None:
        out = extract_sections(_SKILL, ("Background Long Operations",))
        assert "# Agent Rules" in out

    def test_extracted_is_much_smaller_than_the_whole_file(self) -> None:
        out = extract_sections(_SKILL, ("Background Long Operations",))
        assert len(out) < len(_SKILL)

    def test_preserves_multiple_sections_in_file_order(self) -> None:
        out = extract_sections(_SKILL, ("Worktree-First Work", "Background Long Operations"))
        assert out.index("Background Long Operations") < out.index("Worktree-First Work")

    def test_missing_section_raises_not_silently_empty(self) -> None:
        # A typo'd section that silently sent nothing would make the scenario
        # VACUOUS (the agent graded against an empty rule prompt). Fail loud.
        with pytest.raises(MissingSectionError) as exc:
            extract_sections(_SKILL, ("This Section Does Not Exist",))
        assert "This Section Does Not Exist" in str(exc.value)

    def test_one_missing_among_several_still_raises(self) -> None:
        with pytest.raises(MissingSectionError):
            extract_sections(_SKILL, ("Background Long Operations", "Nope"))

    def test_section_match_is_heading_anchored_not_substring(self) -> None:
        # "Questions" appears inside the AskUserQuestion heading; a bare substring
        # match would wrongly pull it. The match is on the ## heading text.
        out = extract_sections(_SKILL, ("Always Use AskUserQuestion for Questions",))
        assert "Never ask inline" in out
        assert "Background anything" not in out
