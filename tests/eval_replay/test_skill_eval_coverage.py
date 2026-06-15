"""Per-skill eval coverage: every skill is covered by >=1 eval or exempt.

Mirrors ``tests/eval_replay/test_trigger_qa.py``'s detect-under / detect-over
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


class TestShippedSkillCoverageEnforced:
    """Phase B: every shipped skill is covered by an eval or carries ``eval_exempt``.

    The shipped corpus is gap-free today (every skill is covered by >=1
    discovered scenario or carries a non-empty ``eval_exempt`` reason), so this
    flips the former warn-first Phase-A guard to the documented Phase-B
    enforcement: a NEW ``skills/<name>/`` that lands with neither an eval nor an
    ``eval_exempt`` frontmatter key is now a hard RED here, not a silent warning.
    The gate is declarative — closing the gap is a one-line ``evals.yaml`` or a
    one-line ``eval_exempt:`` key (see ``evals/README.md`` §
    "Per-skill coverage gate"). ``t3 eval coverage --fail-on-gap`` is the CLI
    counterpart of this gate.
    """

    def test_no_shipped_skill_is_an_uncovered_gap(self) -> None:
        report = skill_eval_coverage()
        gap_names = [r.skill for r in report.gaps]
        assert gap_names == [], (
            "skill(s) ship with neither an eval (skills/<name>/evals.yaml) nor an "
            "`eval_exempt:` frontmatter reason — close each with a co-located eval or a "
            "one-line exemption:\n  " + "\n  ".join(gap_names) + "\n\n" + render_text(report)
        )
