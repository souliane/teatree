"""No skill declares ``subagent_safe`` — the key is superseded and read by nothing.

``t3 <overlay> skill-preamble`` is what makes a sub-agent skill-capable: it
concatenates each SKILL.md body into the dispatch brief, so the agent CARRIES
the skills instead of being certified as able to work without them, and that
contract is enforced (``evals/scenarios/orchestrator_embeds_skills_in_subagent_brief.yaml``).

Frontmatter nobody reads still reads as policy to the next author, so the key
needs a guard or it returns one skill at a time — silently, because
:mod:`teatree.skill_support.schema` validates top-level keys only and treats
``metadata`` as opaque, so no validator would object.
"""

from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

RETIRED_KEY = "subagent_safe"


def _skill_markdown() -> list[Path]:
    return sorted(SKILLS_DIR.rglob("*.md"))


def test_skills_tree_walk_finds_markdown() -> None:
    assert _skill_markdown(), f"nothing matched {SKILLS_DIR}/**/*.md — the walk has drifted, so it can prove nothing"


def test_no_skill_declares_the_superseded_subagent_safe_key() -> None:
    carriers = sorted(
        str(path.relative_to(SKILLS_DIR))
        for path in _skill_markdown()
        if RETIRED_KEY in path.read_text(encoding="utf-8")
    )
    assert not carriers, (
        f"`{RETIRED_KEY}` is superseded by `t3 <overlay> skill-preamble` and read by no code, "
        f"yet these carry it: {carriers}. The preamble embeds each SKILL.md body into the "
        f"sub-agent brief, so a dispatched agent gets the skills themselves — a per-skill "
        f"safe/unsafe flag decides nothing. Drop the key instead of redeclaring it."
    )
