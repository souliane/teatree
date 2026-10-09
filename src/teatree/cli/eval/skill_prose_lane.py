"""``t3 eval skill-prose-judge`` — Tier-3 prose-judge lane (#550, ADVISORY).

The thin CLI surface over the pure :mod:`teatree.eval.skill_prose_judge` engine.
The engine scores each SKILL.md's prose via an injected judge callable and ranks
worst-first; this module wires that callable to the EXISTING ``ClaudeJudge`` seam
(no hand-rolled judge) by synthesising a throwaway judge-only ``EvalSpec`` /
``EvalRun`` per skill and routing it through ``ClaudeJudge.grade`` — the same
clean-room, budget-capped, skip-on-no-``claude`` path the behavioral scenarios'
judge uses. The binary PASS/FAIL verdict maps to a coarse advisory score
(PASS → 1.0, FAIL → 0.0, judge-skipped → ``None``).

Per the campaign's decided philosophy the lane is **ADVISORY**: it logs scores +
nominates the weakest skill but NEVER fails the suite / exits non-zero on a low
score. Matcher / structural lanes gate CI; a judge-only signal is advisory. The
live judge run is metered — the unit tests mock the boundary, the metered lane
drives it for real.
"""

import json
from pathlib import Path

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.cli.eval.verdict import LaneResult
from teatree.eval.judge import ClaudeJudge, JudgeBudget
from teatree.eval.models import EvalRun, EvalSpec, JudgeSpec
from teatree.eval.skill_prose_judge import PROSE_RUBRIC, ProseJudge, ProseJudgeReport, ProseScore, judge_skill_prose
from teatree.utils.django_bootstrap import ensure_django

#: The rubric the binary ``ClaudeJudge`` grades against — a PASS/FAIL framing of
#: the engine's :data:`PROSE_RUBRIC` (the verdict maps to a coarse 1.0/0.0 score).
_JUDGE_PASS_RUBRIC = (
    PROSE_RUBRIC + "\n\nReturn PASS only if the prose is clear and actionable for an AI agent reader "
    "(concrete instructions, an unambiguous rubric, named commands/paths); otherwise FAIL."
)

#: Max judge calls per prose-judge run (cost cap, mirrors the scenario judge default).
_JUDGE_BUDGET = 50


def _claude_prose_judge(budget: JudgeBudget) -> ProseJudge:
    """A :class:`ProseJudge` closure routed through the existing ``ClaudeJudge`` seam."""
    claude_judge = ClaudeJudge(budget=budget)

    def _judge(skill: str, prose: str) -> ProseScore:
        spec = EvalSpec(
            name=f"prose:{skill}",
            scenario=f"the {skill} skill's prose is clear and actionable for an agent reader",
            agent_path=f"skills/{skill}/SKILL.md",
            prompt="",
            matchers=(),
            source_path=Path(f"skills/{skill}/SKILL.md"),
            judge=JudgeSpec(rubric=_JUDGE_PASS_RUBRIC),
        )
        run = EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=(prose,),
            terminal_reason="ok",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
        )
        verdict = claude_judge.grade(spec, run)
        score = None if verdict.skipped else (1.0 if verdict.passed else 0.0)
        return ProseScore(skill=skill, score=score, rationale=verdict.rationale)

    return _judge


def run_prose_judge() -> ProseJudgeReport:
    """Score the shipped skill docs' prose via the live ``ClaudeJudge`` (the lane body)."""
    return judge_skill_prose(_claude_prose_judge(JudgeBudget(max_calls=_JUDGE_BUDGET)))


def skill_prose_judge_lane(report: ProseJudgeReport) -> LaneResult:
    """Fold a prose-judge report into the ADVISORY ``skill-prose-judge`` lane for ``t3 eval``.

    ADVISORY by construction: ``passed`` is ALWAYS ``True`` (a low score never
    fails the suite); the lane SKIPs only when every judge call skipped (no
    ``claude`` / no key). The nomination + score summary lives in ``detail``.
    """
    scored = [s for s in report.scores if s.score is not None]
    all_skipped = bool(report.scores) and not scored
    nominated = report.nominated
    if all_skipped:
        detail = "advisory — judge skipped for every skill (no claude / no key)"
    elif nominated is not None:
        detail = f"advisory — {len(scored)} scored, weakest nominated: {nominated}"
    else:
        detail = "advisory — no skills scored"
    return LaneResult(
        name="skill-prose-judge",
        cost="advisory (judge)",
        passed=True,
        skipped=all_skipped,
        detail=detail,
    )


def skill_prose_judge(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Score each skill's prose for clarity/actionability via the LLM judge (ADVISORY).

    Tier-3 (model-judged): each ``skills/<name>/SKILL.md``'s prose is graded by
    the existing ``ClaudeJudge`` seam and the verdict mapped to a coarse score.
    ADVISORY by design — it ranks the skills worst-first and nominates the weakest
    for a prose pass, but a low score NEVER exits non-zero (matcher / structural
    lanes gate CI; this judge-only signal advises). The judge skips cleanly when
    ``claude`` is not on PATH, so this never blocks a key-less contributor.
    """
    ensure_django()
    require_valid_format(output_format)
    report = run_prose_judge()
    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "advisory": report.advisory,
                    "nominated": report.nominated,
                    "skipped": report.skipped,
                    "scores": [{"skill": s.skill, "score": s.score, "rationale": s.rationale} for s in report.scores],
                },
                indent=2,
            )
        )
    else:
        typer.echo(report.render_text())
    # ADVISORY: never exit non-zero, no matter how low a score is.


__all__ = [
    "run_prose_judge",
    "skill_prose_judge",
    "skill_prose_judge_lane",
]
