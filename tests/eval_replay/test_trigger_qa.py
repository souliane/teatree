"""Trigger-QA validates skill activation against the shipped corpus."""

from pathlib import Path

from teatree.eval.trigger_qa import run_trigger_qa


def _skills_with_corpus(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    debug = skills / "debug"
    debug.mkdir(parents=True)
    (debug / "SKILL.md").write_text(
        "---\nname: debug\ntriggers:\n  keywords:\n    - '\\b(broken|crash|debug)\\b'\n---\n# Debug\n",
        encoding="utf-8",
    )
    return skills


class TestRunTriggerQA:
    def test_shipped_corpus_is_internally_consistent(self) -> None:
        report = run_trigger_qa()
        assert report.checks, "the shipped corpus must define at least one check"
        detail = [f"{c.skill}: {c.prompt!r} should_fire={c.should_fire} fired={c.fired}" for c in report.failures]
        assert report.ok, detail

    def test_detects_under_trigger(self, tmp_path: Path) -> None:
        skills = _skills_with_corpus(tmp_path)
        corpus = tmp_path / "corpus.yaml"
        corpus.write_text(
            "- skill: debug\n  should_fire:\n    - 'this prompt mentions nothing in scope'\n",
            encoding="utf-8",
        )
        report = run_trigger_qa(corpus_path=corpus, skills_dir=skills)
        assert not report.ok
        assert report.failures[0].should_fire is True
        assert report.failures[0].fired is False

    def test_detects_over_trigger(self, tmp_path: Path) -> None:
        skills = _skills_with_corpus(tmp_path)
        corpus = tmp_path / "corpus.yaml"
        corpus.write_text(
            "- skill: debug\n  should_not_fire:\n    - 'the page is broken'\n",
            encoding="utf-8",
        )
        report = run_trigger_qa(corpus_path=corpus, skills_dir=skills)
        assert not report.ok
        assert report.failures[0].should_fire is False
        assert report.failures[0].fired is True

    def test_passes_when_expectations_hold(self, tmp_path: Path) -> None:
        skills = _skills_with_corpus(tmp_path)
        corpus = tmp_path / "corpus.yaml"
        corpus.write_text(
            "- skill: debug\n"
            "  should_fire:\n    - 'the build is broken'\n"
            "  should_not_fire:\n    - 'open a pull request'\n",
            encoding="utf-8",
        )
        report = run_trigger_qa(corpus_path=corpus, skills_dir=skills)
        assert report.ok
        assert len(report.checks) == 2

    def test_missing_skill_dir_yields_no_fire(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus.yaml"
        corpus.write_text(
            "- skill: ghost\n  should_not_fire:\n    - 'anything at all'\n",
            encoding="utf-8",
        )
        report = run_trigger_qa(corpus_path=corpus, skills_dir=tmp_path / "skills")
        assert report.ok
