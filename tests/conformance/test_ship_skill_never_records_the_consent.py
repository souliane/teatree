"""The ship skill never tells the agent to record the approval consent or choose the operator.

``t3 review approve`` spends a consent through the consenting operator's own forge grant. With no
requester on the ticket the consent's approver id picks WHOSE standing grant is spent, so an agent
that records the consent also picks the operator and approves its own work (maker != checker). The
MCP write tools leave ``approve-on-behalf`` out for that reason; the skill text must not hand the
agent the same power through a recipe.
"""

import re
from pathlib import Path

SHIP_DIR = Path(__file__).resolve().parents[2] / "skills" / "ship"

RECORDER = "approve-on-behalf"
FENCE = re.compile(r"^\s*```")


def _recorder_instructions(text: str) -> list[str]:
    """Lines that name the recorder as something to run: a fenced command, or prose with no prohibition."""
    in_fence = False
    offending: list[str] = []
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if RECORDER not in line:
            continue
        runs_it = in_fence and not line.strip().startswith("#")
        if runs_it or "never" not in line.lower():
            offending.append(line)
    return offending


def _ship_markdown() -> list[Path]:
    return sorted(SHIP_DIR.rglob("*.md"))


def test_ship_tree_walk_finds_the_skill() -> None:
    assert SHIP_DIR / "SKILL.md" in _ship_markdown(), f"nothing matched {SHIP_DIR}; the walk proves nothing"


def test_detector_flags_a_recipe_that_records_the_consent() -> None:
    recipe = "```bash\nt3 review approve-on-behalf org/repo!7 approve --approver gitlab:alice\n```\n"
    prose = "Then run `t3 review approve-on-behalf org/repo!7 approve` yourself.\n"
    commented = "```bash\n# never Y — recording the consent yourself (approve-on-behalf)\n```\n"

    assert _recorder_instructions(recipe)
    assert _recorder_instructions(prose)
    assert not _recorder_instructions(commented)


def test_no_ship_skill_file_tells_the_agent_to_run_approve_on_behalf() -> None:
    offenders = {
        str(path.relative_to(SHIP_DIR)): lines
        for path in _ship_markdown()
        if (lines := _recorder_instructions(path.read_text(encoding="utf-8")))
    }

    assert not offenders, (
        f"these ship skill lines tell the agent to run `t3 review {RECORDER}`: {offenders}. The person "
        "records the consent; the agent shows them the command the refusal names and never picks the operator."
    )


def test_ship_skill_says_the_person_records_the_consent() -> None:
    text = (SHIP_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "the PERSON records the consent" in text
    assert "never record the consent or pick the operator" in text
