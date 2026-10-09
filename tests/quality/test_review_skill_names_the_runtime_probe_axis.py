"""The review skill's diff guidance also names the RUNTIME-probe axis (#4251).

The three-dot guidance covers what the branch introduced; it said nothing about
which tree a reviewer imports and measures on. A reviewer following it exactly
still probed the branch checkout and blocked a docs-only PR on a `src/` finding
about code it does not touch — so the axis has to be named, together with the
three probe environments that have each produced a confident wrong answer here.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_REVIEW_SKILL = _ROOT / "skills" / "review" / "SKILL.md"

_SECTION_HEADING_RE = re.compile(r"^####\s+Two Axes: Read the Diff Three-Dot, MEASURE on the Merge Result\b")
_NEXT_SECTION_RE = re.compile(r"^#{1,4}\s")


def _runtime_axis_section() -> str:
    lines: list[str] = []
    in_section = False
    in_fence = False
    for line in _REVIEW_SKILL.read_text(encoding="utf-8").splitlines():
        if not in_section:
            if _SECTION_HEADING_RE.match(line):
                in_section = True
            continue
        if line.startswith("```"):
            in_fence = not in_fence
        # A shell comment inside a fence starts with `#` too — only an unfenced
        # heading ends the section.
        elif not in_fence and _NEXT_SECTION_RE.match(line):
            break
        lines.append(line)
    return "\n".join(lines)


def test_the_runtime_probe_section_exists() -> None:
    assert _runtime_axis_section(), "skills/review/SKILL.md no longer names the runtime-probe axis"


def test_it_prescribes_the_one_step_merge_result_extract() -> None:
    assert "t3 review merge-tree" in _runtime_axis_section()


def test_it_forbids_each_probe_environment_that_produced_a_false_result() -> None:
    section = _runtime_axis_section()

    assert "worktree" in section, "the git-worktree trap (per-worktree DB isolation) is unnamed"
    assert "git archive" in section, "the no-.git extract trap is unnamed"
    assert "origin" in section, "the local-path origin trap is unnamed"


def test_it_names_the_record_time_gate_and_its_attestation() -> None:
    section = _runtime_axis_section()

    assert "changed-file set" in section
    assert "--merge-result-retake" in section
