"""The retro skill tree may not instruct a write to an assistant memory file.

The runtime half of this invariant is already pinned by
``tests/test_no_agent_memory_dependency.py`` (#3277): an assistant's ``MEMORY.md``
is a convenience for the assistant, never a functional input to the factory. This
is the OUTPUT half — retro must not produce one either. A rule that recurred once
is an enforcement gap, and re-persisting the same prose is what makes it recur
again; the durable homes retro is left with are an editable skill file and
``t3 <overlay> retro finding``, which lands the gap as a scheduled, merging fix.

Two teeth, both read from the shipped prose rather than from a summary of it:

*   a hard ban on the assistant-memory PATHS — naming one is a discovery recipe,
    and the retro skill has no remaining reason to hold it;
*   a write-instruction ban — a write verb plus a memory noun in DESTINATION
    position, judged per CLAUSE. A clause that negates escapes, so "retro never
    writes a memory file" stays sayable while an instruction appended to it does not.

Reading a memory corpus stays legal on purpose: § 9 still scans for behaviour
encoded outside the framework, and the weekly ``memory_skim`` mini-loop owns the
corpus. Neither tooth fires on a line that only names where a rule lives.
"""

import re
from pathlib import Path

import pytest

_RETRO_SKILL_DIR = Path(__file__).parents[2] / "skills" / "retro"

#: Assistant-memory paths — banned outright, with no prohibition escape.
_FORBIDDEN_PATHS = ("~/.claude/projects", ".claude/projects", "MEMORY.md", "MEMORY_ARCHIVE.md")

#: Verbs that PRODUCE memory prose. Reading, scanning and promoting *out of* a
#: memory are deliberately absent — those remain legitimate retro work.
_WRITE_VERB = re.compile(
    r"\b(write(?:s)?|writing|written|append(?:s|ing|ed)?|save(?:s|d)?|saving"
    r"|record(?:s|ing|ed)?|add(?:s|ing|ed)?|persist(?:s|ing|ed|ence)?|remind(?:s|ing|er|ers)?"
    r"|store(?:s|d)?|storing|note(?:s|d)?|noting|log(?:s|ged|ging)?"
    r"|captur(?:e|es|ed|ing)|creat(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)

#: A memory noun in destination position: the object of a "to/into/in", or the
#: head of a bare noun phrase ("the memory", "a personal memory"). The clause
#: separators are excluded so the match cannot span two sentences or table cells.
#: ``in-memory`` names process memory, a different noun the corpus rules never reach.
_MEMORY_TARGET = re.compile(
    r"\b(?:to|into|in)\b[^.;|]{0,110}?(?<!in-)\bmemor(?:y|ies)\b"
    r"|\b(?:the|a|an|another|one|personal|user|agent)\b[^.;|]{0,20}?(?<!in-)\bmemor(?:y|ies)\b",
    re.IGNORECASE,
)

#: An explicit negation, or a bare "not" whose object IS the memory noun.
_PROHIBITION = re.compile(
    r"\b(never|no longer|nothing|none|instead of|rather than|refus\w*|neither|nor)\b"
    r"|\bnot\b[^.;|]{0,30}?\bmemor(?:y|ies)\b",
    re.IGNORECASE,
)

#: Verb, destination AND negation are all judged per clause. Line-wide, a write verb in
#: one sentence paired with a memory noun in the next; line-wide prohibition, one `never`
#: anywhere exempted an instruction appended after it — and this prose is dense with them.
_CLAUSE = re.compile(r"[.;|]")


def _skill_documents() -> list[Path]:
    return sorted(_RETRO_SKILL_DIR.rglob("*.md"))


def _instructs_a_memory_write(line: str) -> bool:
    return any(
        _WRITE_VERB.search(clause) and _MEMORY_TARGET.search(clause) and not _PROHIBITION.search(clause)
        for clause in _CLAUSE.split(line)
    )


def test_the_retro_skill_names_no_assistant_memory_path() -> None:
    found = {
        f"{path.relative_to(_RETRO_SKILL_DIR)}: {token}"
        for path in _skill_documents()
        for token in _FORBIDDEN_PATHS
        if token in path.read_text(encoding="utf-8")
    }
    assert not found, (
        "the retro skill names an assistant-memory path — retro discovers and writes no memory file, "
        f"and the corpus belongs to the weekly memory_skim loop: {sorted(found)}"
    )


def test_no_retro_instruction_writes_a_memory() -> None:
    offenders = [
        f"{path.relative_to(_RETRO_SKILL_DIR)}: {line.strip()}"
        for path in _skill_documents()
        for line in path.read_text(encoding="utf-8").splitlines()
        if _instructs_a_memory_write(line)
    ]
    assert not offenders, (
        "the retro skill instructs a memory write — a finding's durable homes are an editable skill "
        f"file and `t3 <overlay> retro finding`: {offenders}"
    )


def test_the_gate_catches_a_reintroduced_memory_instruction() -> None:
    """The teeth proof: the instruction shapes this change removed still red."""
    assert _instructs_a_memory_write("- If a finding is user-specific, write it to the memory file now.")
    assert _instructs_a_memory_write("Writing the memory entry is the appropriate fix.")


def test_the_gate_leaves_a_prohibition_and_a_read_alone() -> None:
    """The escapes the doctrine needs: stating the rule, and naming where a rule lives."""
    assert not _instructs_a_memory_write("Retro never writes a memory file.")
    assert not _instructs_a_memory_write("An equivalent rule lives in a skill or memory file and recurred anyway.")


def test_a_write_verb_in_a_neighbouring_sentence_is_not_an_instruction() -> None:
    assert not _instructs_a_memory_write("Section 7 says who owns the memory corpus. Patterns added manually.")


@pytest.mark.parametrize(
    "line",
    [
        "Store the finding in the user's memory file.",
        "Note the guardrail in the agent memory file.",
        "Log the lesson to the memory index.",
        "Capture the correction in a personal memory file.",
        "Create a memory entry for the rule.",
    ],
)
def test_every_producing_verb_is_covered_not_just_write(line: str) -> None:
    assert _instructs_a_memory_write(line)


@pytest.mark.parametrize(
    "line",
    [
        "Retro never deletes an entry; write the finding into the memory file now.",
        "Never prune an entry. Add the rule to the user memory file.",
    ],
)
def test_a_prohibition_exempts_its_own_clause_only(line: str) -> None:
    assert _instructs_a_memory_write(line)


def test_an_in_memory_read_is_not_the_memory_corpus() -> None:
    """Process memory is a different noun — a RED harness may write after one."""
    assert not _instructs_a_memory_write("Save from a possibly-stale in-memory read, then write.")
