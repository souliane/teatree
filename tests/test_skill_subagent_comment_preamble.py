"""#1531 — near-zero-comments preamble must survive sub-agent dispatch.

The minimal-comments rule lives in skill prose, but prose does not propagate
into a spawned sub-agent's context. The skills that dispatch
code-writing/shipping/reviewing sub-agents therefore carry the preamble
verbatim and inline in their documented dispatch templates. This guard fails
RED if any of those templates drops it, so the rule can't silently regress
back to a "remember to add it" note.
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
    "teatree-batch",
    "code",
    "ship",
)


def _normalize(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("skill", DISPATCH_SKILLS)
def test_dispatch_template_inlines_the_preamble(skill: str) -> None:
    prose = (REPO_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert _normalize(PREAMBLE) in _normalize(prose), (
        f"skills/{skill}/SKILL.md no longer inlines the near-zero-comments "
        "preamble in its sub-agent dispatch template (souliane/teatree#1531). "
        "The rule does not propagate to spawned agents through prose — keep it "
        "verbatim in the dispatch prompt."
    )
