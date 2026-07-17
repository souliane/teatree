"""Per-skill eval coverage: a skill ships >=1 behavioral eval, or is exempt.

A skill is COVERED when ``discover_specs()`` yields >=1 spec whose resolved
``agent_path`` is ``skills/<name>/SKILL.md``. Scenarios live in the single
``evals/scenarios/`` catalog (plus each overlay's own dir); a scenario targets a
skill purely through its ``agent_path``, so the gate is decoupled from where the
YAML file sits — it forbids only zero behavioral coverage with no exemption.

A skill is EXEMPT when its ``SKILL.md`` frontmatter carries a non-empty
``eval_exempt: <reason>`` (pure-doc / methodology skills). The reason lives WITH
the skill (declarative, self-documenting). ``skill_schema`` validates the key is
a non-empty string; this module only reads it.

A skill that is neither covered nor exempt is a GAP. The coverage report is a
pure function over ``discover_specs()`` + frontmatter — deterministic, free, no
model — feeding ``t3 eval coverage`` and the warn-first per-PR gate.
"""

import dataclasses
import json
import re
from collections.abc import Iterable
from pathlib import Path

from teatree.eval.discovery import DEFAULT_SKILLS_DIR, discover_specs
from teatree.eval.models import AnyOf, EvalSpec, FinalStateMatcher, Matcher

_AGENT_PATH = re.compile(r"^skills/(?P<skill>[^/]+)/SKILL\.md$")
_EVAL_EXEMPT_LINE = re.compile(r"^eval_exempt:\s*(?P<reason>.*)$")


@dataclasses.dataclass(frozen=True)
class SkillCoverage:
    skill: str
    covered: bool
    scenario_count: int
    exempt: bool
    exempt_reason: str | None

    @property
    def is_gap(self) -> bool:
        return not self.covered and not self.exempt


@dataclasses.dataclass(frozen=True)
class CoverageReport:
    rows: tuple[SkillCoverage, ...]

    @property
    def by_skill(self) -> dict[str, SkillCoverage]:
        return {row.skill: row for row in self.rows}

    @property
    def gaps(self) -> tuple[SkillCoverage, ...]:
        return tuple(row for row in self.rows if row.is_gap)


def _skill_of(agent_path: str) -> str | None:
    match = _AGENT_PATH.match(agent_path.strip())
    return match.group("skill") if match else None


def _has_positive_teeth(spec: EvalSpec) -> bool:
    """True when *spec* carries at least one matcher that ASSERTS a behavior.

    Coverage counts only scenarios with a positive-teeth matcher — a positive
    ``tool_call``, a ``final_state``, or an ``any_of`` disjunction. A judge-only
    (matcherless) spec has no deterministic teeth, and a purely negative spec only
    forbids a call; neither, alone, is real behavioral coverage a default lane can
    gate on. This composes with the report's judge-only skip (#3313): a skill
    "covered" only by a judge-only or negative-only scenario is a GAP, not green.
    """
    return any(
        isinstance(matcher, (FinalStateMatcher, AnyOf)) or (isinstance(matcher, Matcher) and matcher.kind == "positive")
        for matcher in spec.matchers
    )


def _eval_exempt_reason(skill_md: Path) -> str | None:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    try:
        frontmatter = text[3 : text.index("---", 3)]
    except ValueError:
        return None
    for line in frontmatter.splitlines():
        match = _EVAL_EXEMPT_LINE.match(line)
        if match:
            reason = match.group("reason").strip().strip("'\"")
            return reason or None
    return None


def skill_eval_coverage(
    skills_dir: Path = DEFAULT_SKILLS_DIR,
    specs: Iterable[EvalSpec] | None = None,
) -> CoverageReport:
    if specs is None:
        specs = discover_specs()
    counts: dict[str, int] = {}
    for spec in specs:
        skill = _skill_of(spec.agent_path)
        if skill is not None and _has_positive_teeth(spec):
            counts[skill] = counts.get(skill, 0) + 1
    rows: list[SkillCoverage] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        name = skill_md.parent.name
        reason = _eval_exempt_reason(skill_md)
        scenario_count = counts.get(name, 0)
        rows.append(
            SkillCoverage(
                skill=name,
                covered=scenario_count > 0,
                scenario_count=scenario_count,
                exempt=reason is not None,
                exempt_reason=reason,
            )
        )
    return CoverageReport(rows=tuple(rows))


def render_text(report: CoverageReport) -> str:
    lines = [f"{'SKILL':<22} {'EVALS':>5}  {'STATUS'}"]
    for row in report.rows:
        if row.covered:
            status = "covered"
        elif row.exempt:
            status = f"exempt ({row.exempt_reason})"
        else:
            status = "GAP — no eval, no eval_exempt"
        lines.append(f"{row.skill:<22} {row.scenario_count:>5}  {status}")
    covered = sum(1 for r in report.rows if r.covered)
    exempt = sum(1 for r in report.rows if r.exempt and not r.covered)
    lines.append(
        f"\nsummary: {covered} covered, {exempt} exempt, {len(report.gaps)} gap(s) of {len(report.rows)} skills"
    )
    return "\n".join(lines)


def render_json(report: CoverageReport) -> str:
    return json.dumps(
        {
            "gaps": [r.skill for r in report.gaps],
            "skills": [
                {
                    "skill": r.skill,
                    "covered": r.covered,
                    "scenario_count": r.scenario_count,
                    "exempt": r.exempt,
                    "exempt_reason": r.exempt_reason,
                }
                for r in report.rows
            ],
        },
        indent=2,
    )
