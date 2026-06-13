"""#550 Tier-3 engine: model-judged skill-prose clarity/actionability (ADVISORY).

The engine scores each ``skills/<name>/SKILL.md``'s prose for clarity and
actionability via an injected judge callable (the real lane wires the existing
``ClaudeJudge`` seam; here the judge is MOCKED — no metered call). Per the
campaign philosophy Tier-3 is ADVISORY: it logs/scores + nominates the weakest
skill, but a low score NEVER raises or makes the lane exit non-zero. Matcher /
structural lanes gate CI; a judge-only lane is advisory.
"""

from pathlib import Path

import pytest

from teatree.eval.skill_prose_judge import ProseScore, judge_skill_prose


def _skill(skills_dir: Path, name: str, body: str) -> None:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")


def _fixed_judge(scores: dict[str, float]) -> object:
    """A mock judge callable: returns the mapped score (default mid) per skill."""

    def _judge(skill: str, prose: str) -> ProseScore:
        return ProseScore(skill=skill, score=scores.get(skill, 0.5), rationale=f"mock:{skill}")

    return _judge


class TestJudgeSkillProse:
    def test_scores_every_skill_and_ranks_lowest_first(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "alpha", "clear and actionable")
        _skill(skills, "beta", "vague")
        _skill(skills, "gamma", "ok")
        report = judge_skill_prose(
            _fixed_judge({"alpha": 0.9, "beta": 0.2, "gamma": 0.6}),
            skills_dir=skills,
        )
        assert [s.skill for s in report.scores] == ["beta", "gamma", "alpha"]
        assert report.scores[0].score == pytest.approx(0.2)

    def test_nominates_the_weakest_skill(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "alpha", "clear")
        _skill(skills, "beta", "vague")
        report = judge_skill_prose(_fixed_judge({"alpha": 0.9, "beta": 0.1}), skills_dir=skills)
        assert report.nominated == "beta"

    def test_low_score_is_advisory_never_raises(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "weak", "terrible prose")
        # the whole point of advisory: a 0.0 score does not raise / signal failure
        report = judge_skill_prose(_fixed_judge({"weak": 0.0}), skills_dir=skills)
        assert report.advisory is True
        assert report.scores[0].score == pytest.approx(0.0)

    def test_skipped_judge_does_not_count_as_failure(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "x", "prose")

        def _skipping_judge(skill: str, prose: str) -> ProseScore:
            return ProseScore(skill=skill, score=None, rationale="judge skipped (no claude)")

        report = judge_skill_prose(_skipping_judge, skills_dir=skills)
        assert report.advisory is True
        assert report.skipped == 1
        assert report.nominated is None  # nothing scored → nothing to nominate

    def test_empty_skills_dir_is_a_clean_no_op(self, tmp_path: Path) -> None:
        report = judge_skill_prose(_fixed_judge({}), skills_dir=tmp_path / "skills")
        assert report.scores == ()
        assert report.nominated is None
        assert report.advisory is True

    def test_render_text_shows_scores_and_nomination(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        _skill(skills, "alpha", "clear")
        _skill(skills, "beta", "vague")
        report = judge_skill_prose(_fixed_judge({"alpha": 0.9, "beta": 0.2}), skills_dir=skills)
        rendered = report.render_text()
        assert "beta" in rendered
        assert "ADVISORY" in rendered
        assert "nominate" in rendered.lower()
