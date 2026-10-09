"""#1531 — near-zero-comments preamble must survive sub-agent dispatch.

The minimal-comments rule lives in skill prose, but prose does not propagate
into a spawned sub-agent's context. The skills that dispatch
code-writing/shipping/reviewing sub-agents therefore carry the preamble
verbatim and inline in their documented dispatch templates. This guard fails
RED if any of those templates drops it, so the rule can't silently regress
back to a "remember to add it" note.

The match is an EXACT substring against the canonical ``PREAMBLE`` — no
whitespace or backslash normalization. A line-broken or backslash-escaped
copy (e.g. inlined into a quoted ``Agent(prompt: "…")`` literal) is not the
same byte sequence and must fail RED, so all three skills keep one canonical
verbatim block.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

PREAMBLE = (
    "NEAR-ZERO COMMENTS: names + types are the documentation. Do NOT add "
    "comments that restate the code. NO comments referencing "
    "MRs/tickets/workstreams/Slack threads. Rationale belongs in the commit "
    "message, never inline."
)

DISPATCH_SKILLS = (
    "wip",
    "code",
    "ship",
)


@pytest.mark.parametrize("skill", DISPATCH_SKILLS)
def test_dispatch_template_inlines_the_preamble_verbatim(skill: str) -> None:
    prose = (REPO_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert PREAMBLE in prose, (
        f"skills/{skill}/SKILL.md does not contain the canonical near-zero-comments "
        "preamble byte-for-byte (souliane/teatree#1531). The rule does not propagate "
        "to spawned agents through prose, and a line-broken or backslash-escaped copy "
        "is not the same verbatim block — keep one canonical fenced block in every "
        "dispatch template."
    )
