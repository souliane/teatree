"""skill-prose-judge: Tier-3 model-judged skill-prose eval (#550) — ADVISORY.

Tier-1 (command-validity) and the matcher/structural lanes gate CI
deterministically. Tier-3 scores something a matcher cannot: is a skill's PROSE
clear and actionable to the agent that reads it? It hands each
``skills/<name>/SKILL.md`` to a judge (the real lane wires the existing
``ClaudeJudge`` seam — this engine never hand-rolls a judge) and reads back a
0..1 clarity/actionability score.

Per the campaign's decided philosophy this lane is **ADVISORY**: it scores,
ranks worst-first, and nominates the weakest skill for a prose pass — but a low
score NEVER raises and NEVER makes the lane exit non-zero. A judge-only signal
is too soft to gate CI deterministically; the matcher/structural lanes do that.
``advisory`` is therefore always ``True`` on the report — the field is the
contract a caller asserts on, so the lane can be wired to never fail the suite.

The engine is pure and judge-inverted: the judge is the injected
``Callable[[skill, prose], ProseScore]`` so unit tests mock the boundary (no
metered call). The live metered judge run happens through the metered lane / the
CLI wiring, not in unit tests.
"""

import dataclasses
from collections.abc import Callable
from pathlib import Path

# ``skills/`` sits next to ``src/`` — resolve from this module's path so the lane
# stays a leaf of the eval package (the backwards-edge convention the sibling
# ``discovery`` / ``trigger_qa`` / ``skill_command_validity`` lanes follow).
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"

#: The clarity/actionability rubric the live judge grades each SKILL.md against.
#: Kept here (not in the CLI lane) so the rubric travels with the engine and the
#: mock-judged unit tests document the same contract the metered run uses.
PROSE_RUBRIC = (
    "Score this skill's prose for an AI agent reader on a 0.0-1.0 scale, where 1.0 is "
    "maximally clear and actionable. Reward: concrete imperative instructions, an "
    "unambiguous decision rubric, named CLI commands / file paths, and a self-evident "
    "structure. Penalize: vague exhortation with no action, contradictory guidance, "
    "and rules an agent cannot operationalize. Reply with the score and a one-sentence "
    "reason."
)


@dataclasses.dataclass(frozen=True)
class ProseScore:
    """One skill's judged prose score. ``score is None`` means the judge skipped."""

    skill: str
    score: float | None
    rationale: str


ProseJudge = Callable[[str, str], ProseScore]


@dataclasses.dataclass(frozen=True)
class ProseJudgeReport:
    scores: tuple[ProseScore, ...]
    skipped: int

    #: Tier-3 is advisory BY CONSTRUCTION — this is always True so a caller can
    #: wire the lane to never fail the suite, no matter how low a score is.
    advisory: bool = True

    @property
    def nominated(self) -> str | None:
        """The weakest scored skill (lowest score), or ``None`` if none scored."""
        scored = [s for s in self.scores if s.score is not None]
        if not scored:
            return None
        return min(scored, key=lambda s: s.score or 0.0).skill

    def render_text(self) -> str:
        lines = ["skill-prose-judge (ADVISORY — scores + nominates, never fails the suite):"]
        for s in self.scores:
            shown = "skipped" if s.score is None else f"{s.score:.2f}"
            lines.append(f"  {s.skill:<24} {shown:>7}  {s.rationale}")
        nominated = self.nominated
        if nominated is not None:
            lines.append(f"\nnominate for a prose pass (weakest): {nominated}")
        if self.skipped:
            lines.append(f"({self.skipped} skill(s) judge-skipped — no metered call)")
        return "\n".join(lines)


def _read_prose(skill_md: Path) -> str:
    """The SKILL.md body with its YAML frontmatter stripped (judge the prose, not the metadata)."""
    text = skill_md.read_text(encoding="utf-8")
    if text.startswith("---"):
        try:
            return text[text.index("---", 3) + 3 :].lstrip()
        except ValueError:
            return text
    return text


def judge_skill_prose(judge: ProseJudge, *, skills_dir: Path = DEFAULT_SKILLS_DIR) -> ProseJudgeReport:
    """Score every ``skills/<name>/SKILL.md``'s prose via *judge*, ranked worst-first.

    *judge* is the injected boundary — the live lane passes a closure over
    ``ClaudeJudge``; unit tests pass a mock. The report is ADVISORY: scores are
    sorted lowest-first, the weakest is nominated, a skipped judge is counted but
    never a failure, and ``advisory`` is always ``True``.
    """
    scores: list[ProseScore] = []
    skipped = 0
    if skills_dir.is_dir():
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            skill = skill_md.parent.name
            result = judge(skill, _read_prose(skill_md))
            if result.score is None:
                skipped += 1
            scores.append(result)
    ranked = tuple(sorted(scores, key=lambda s: (s.score is None, s.score if s.score is not None else 0.0)))
    return ProseJudgeReport(scores=ranked, skipped=skipped)
