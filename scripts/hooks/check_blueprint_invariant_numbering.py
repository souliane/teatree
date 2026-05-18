"""Pre-commit hook: BLUEPRINT.md §17.1 invariant-numbering integrity.

Background (souliane/teatree#836, §17.6 gate family): concurrent PRs each
appended "the next" §17.1 invariant number against a stale base, so two
PRs both added invariant ``6`` (or ``7``) and the merge silently dropped
or duplicated one. This recurred three times in one session (#856/#859,
#859/#863). Memory/vigilance does not catch it — a deterministic gate
evaluated on the merge result does.

The gate parses the numbered invariant list under ``### 17.1 Invariants``
in ``BLUEPRINT.md`` and FAILS when the numbers are not a gapless ``1..N``
sequence with no repeats. It runs at the same pre-commit/prek layer as
the existing ``blueprint-sync`` / ``skill-prose-ban`` checks, so it fires
on whatever tree is being committed — including the merge-result tree.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The list lives under "### 17.1 Invariants" and ends at the next "### "
# heading (the next subsection — e.g. "### 17.2 The flywheel"). Each
# invariant is a top-level numbered list item: ``N. **Title.**``.
_SECTION_HEADING_RE = re.compile(r"^###\s+17\.1\s+Invariants\s*$")
_NEXT_SUBSECTION_RE = re.compile(r"^###\s")
_INVARIANT_ITEM_RE = re.compile(r"^(\d+)\.\s+\*\*")


@dataclass(frozen=True)
class NumberingResult:
    numbers: list[int]
    ok: bool
    reason: str


def _blueprint_path() -> Path:
    return Path(__file__).resolve().parents[2] / "BLUEPRINT.md"


def extract_invariant_numbers(blueprint_text: str) -> list[int]:
    """Return the ordered list of §17.1 numbered-invariant markers.

    Reads only the block between the ``### 17.1 Invariants`` heading and
    the next ``### `` subsection heading, so numbered lists elsewhere in
    the document do not contaminate the check.
    """
    numbers: list[int] = []
    in_section = False
    for line in blueprint_text.splitlines():
        if not in_section:
            if _SECTION_HEADING_RE.match(line):
                in_section = True
            continue
        if _NEXT_SUBSECTION_RE.match(line):
            break
        match = _INVARIANT_ITEM_RE.match(line)
        if match is not None:
            numbers.append(int(match.group(1)))
    return numbers


def check_numbering(numbers: list[int]) -> NumberingResult:
    """Validate that *numbers* is a gapless ``1..N`` sequence, no repeats."""
    if not numbers:
        return NumberingResult(
            numbers=numbers,
            ok=False,
            reason="No numbered invariants found under '### 17.1 Invariants'.",
        )

    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        return NumberingResult(
            numbers=numbers,
            ok=False,
            reason=(
                f"Duplicate invariant number(s) {duplicates} in §17.1 "
                f"(parsed sequence: {numbers}). Two concurrent PRs likely "
                "appended the same next number against a stale base — "
                "renumber so the list is a gapless 1..N with no repeats."
            ),
        )

    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        return NumberingResult(
            numbers=numbers,
            ok=False,
            reason=(
                f"§17.1 invariants are not contiguous: parsed {numbers}, "
                f"expected {expected}. A merge silently dropped or "
                "reordered an invariant — renumber to a gapless 1..N."
            ),
        )

    return NumberingResult(numbers=numbers, ok=True, reason="")


def _blueprint_in_commit() -> bool:
    """True when BLUEPRINT.md is part of the staged change.

    The numbering invariant only needs re-checking when the file (or its
    merge result) is actually being committed; an unrelated commit must
    not be blocked by pre-existing drift it did not introduce.
    """
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "BLUEPRINT.md" in result.stdout.splitlines()


def main() -> int:
    if not _blueprint_in_commit():
        return 0

    path = _blueprint_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Missing/unreadable BLUEPRINT — nothing this gate can assert;
        # fail open rather than block on a broken tree.
        return 0

    result = check_numbering(extract_invariant_numbers(text))
    if result.ok:
        return 0

    print()
    print("  BLUEPRINT.md §17.1 invariant-numbering integrity FAILED:")
    print()
    print(f"    {result.reason}")
    print()
    print("    Recurring collision class (#836 §17.6 gate 1): concurrent")
    print("    PRs append the same 'next' invariant number against a stale")
    print("    base. Renumber §17.1 to a gapless 1..N before committing.")
    print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
