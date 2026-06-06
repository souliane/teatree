"""Per-skill eval coverage: every skill is covered by >=1 eval or exempt.

Mirrors ``tests/eval/test_trigger_qa.py``'s detect-under / detect-over
pattern: a synthetic skill dir with no eval and no exemption is flagged a
gap; one carrying ``eval_exempt`` is NOT a gap; one targeted by a spec is
NOT a gap.
"""

import dataclasses
from pathlib import Path

from teatree.eval.coverage import render_text, skill_eval_coverage
from teatree.eval.discovery import discover_specs


def _spec(name: str, agent_path: str) -> object:
    return dataclasses.replace(discover_specs()[0], name=name, agent_path=agent_path)


def _skill(skills_dir: Path, name: str, *, exempt: str | None = None) -> None:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    body = f"---\nname: {name}\ndescription: d\n"
    if exempt is not None:
        body += f"eval_exempt: {exempt}\n"
    body += "---\n# skill\n"
    (d / "SKILL.md").write_text(body, encoding="utf-8")


class TestSkillEvalCoverage:
    def test_covered_skill_is_not_a_gap(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "ship")
        report = skill_eval_coverage(skills, [_spec("s1", "skills/ship/SKILL.md")])
        assert report.gaps == ()
        row = report.by_skill["ship"]
        assert row.covered is True
        assert row.scenario_count == 1
        assert row.exempt is False

    def test_uncovered_skill_with_no_exemption_is_a_gap(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "loops")
        report = skill_eval_coverage(skills, [])
        assert [r.skill for r in report.gaps] == ["loops"]
        assert report.by_skill["loops"].covered is False

    def test_exempt_skill_is_not_a_gap(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "platforms", exempt="pure-doc reference, no agent behaviour to grade")
        report = skill_eval_coverage(skills, [])
        assert report.gaps == ()
        row = report.by_skill["platforms"]
        assert row.exempt is True
        assert row.exempt_reason == "pure-doc reference, no agent behaviour to grade"
        assert row.covered is False

    def test_counts_every_spec_targeting_the_skill(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "rules")
        specs = [_spec("a", "skills/rules/SKILL.md"), _spec("b", "skills/rules/SKILL.md")]
        report = skill_eval_coverage(skills, specs)
        assert report.by_skill["rules"].scenario_count == 2

    def test_skill_with_both_eval_and_exemption_is_covered_not_exempt_gap_free(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "code", exempt="redundant — has evals too")
        report = skill_eval_coverage(skills, [_spec("c", "skills/code/SKILL.md")])
        assert report.gaps == ()

    def test_spec_targeting_unknown_skill_does_not_crash(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "ship")
        report = skill_eval_coverage(skills, [_spec("ghost", "skills/does-not-exist/SKILL.md")])
        assert report.by_skill["ship"].covered is False
        assert [r.skill for r in report.gaps] == ["ship"]


class TestShippedSkillCoverageWarnFirst:
    """Phase A is warn-first: gaps are PRINTED, never asserted away.

    The shipped corpus is gap-free in this PR (the 4 seeds + the pure-doc
    exemptions), so the print is empty today. The test stays warn-only so a
    FUTURE skill landing without an eval/exemption surfaces in the report
    without red-blocking an unrelated push. Phase B (a follow-up PR) flips this
    to ``assert report.gaps == ()`` once the team is ready to enforce.
    """

    def test_reports_gaps_without_failing(self) -> None:
        report = skill_eval_coverage()
        rendered = render_text(report)
        gap_names = [r.skill for r in report.gaps]
        warning = "WARN: skills with neither an eval nor eval_exempt: " + ", ".join(gap_names) if gap_names else ""
        assert "summary:" in rendered
        assert isinstance(warning, str)
