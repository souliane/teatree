"""Every judge-bearing scenario's rubric criteria are classified, or it is a gap.

Mirrors ``test_skill_eval_coverage.py``'s shape: synthetic-catalog unit tests for
the schema + gap/stale logic, plus one shipped-corpus enforcement test that reads
the REAL ``evals/judge_rubric_classification.yaml`` against the REAL
``discover_specs()`` — closing the silent-regrowth gap issue #4819 names: a new
judge rubric added with no classification entry fails loud here.
"""

import dataclasses
from pathlib import Path

import pytest
import yaml

from teatree.eval.discovery import discover_specs
from teatree.eval.judge_rubric_classification import (
    CLASSIFICATION_PATH,
    JudgeRubricClassificationError,
    judge_rubric_coverage,
)
from teatree.eval.models import JudgeSpec


def _judge_spec(name: str) -> object:
    # A judge-bearing spec built off a real discovered one, so every OTHER
    # required EvalSpec field stays valid — only `name`/`judge` are varied.
    return dataclasses.replace(discover_specs()[0], name=name, judge=JudgeSpec(rubric="r"))


def _write_classification(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _entry(*, criterion: str = "c", criterion_class: str = "holistic", **extra: object) -> dict[str, object]:
    return {"criteria": [{"criterion": criterion, "class": criterion_class, "reason": "because", **extra}]}


class TestJudgeRubricCoverage:
    def test_classified_spec_is_not_a_gap(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {"scenario_a": _entry()})
        report = judge_rubric_coverage([_judge_spec("scenario_a")], classification_path=path)
        assert report.gaps == ()
        assert report.is_clean is True
        assert [r.spec_name for r in report.rows] == ["scenario_a"]

    def test_unclassified_judge_bearing_spec_is_a_gap(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {})
        report = judge_rubric_coverage([_judge_spec("scenario_a")], classification_path=path)
        assert report.gaps == ("scenario_a",)
        assert report.is_clean is False

    def test_deleting_an_entry_re_fails_it(self, tmp_path: Path) -> None:
        """The real classification file, minus one entry, must re-gap that spec."""
        real = yaml.safe_load(CLASSIFICATION_PATH.read_text(encoding="utf-8"))
        name = next(iter(real))
        thinned = {k: v for k, v in real.items() if k != name}
        path = tmp_path / "classification.yaml"
        _write_classification(path, thinned)
        report = judge_rubric_coverage([_judge_spec(name)], classification_path=path)
        assert name in report.gaps

    def test_entry_for_a_non_judge_bearing_spec_is_stale_not_a_gap(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {"ghost_scenario": _entry()})
        report = judge_rubric_coverage([], classification_path=path)
        assert report.gaps == ()
        assert report.stale == ("ghost_scenario",)

    def test_non_judge_bearing_spec_needs_no_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {})
        matcherless = dataclasses.replace(discover_specs()[0], name="no_judge", judge=None)
        report = judge_rubric_coverage([matcherless], classification_path=path)
        assert report.gaps == ()

    def test_assertion_migrated_true_requires_a_matcher_name(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {"scenario_a": _entry(criterion_class="assertion", migrated=True)})
        with pytest.raises(JudgeRubricClassificationError, match="matcher"):
            judge_rubric_coverage([_judge_spec("scenario_a")], classification_path=path)

    def test_assertion_migrated_false_needs_no_matcher(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {"scenario_a": _entry(criterion_class="assertion", migrated=False)})
        report = judge_rubric_coverage([_judge_spec("scenario_a")], classification_path=path)
        criterion = report.rows[0].criteria[0]
        assert criterion.migrated is False
        assert criterion.matcher is None

    def test_non_assertion_criterion_rejects_a_migrated_key(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {"scenario_a": _entry(criterion_class="holistic", migrated=False)})
        with pytest.raises(JudgeRubricClassificationError, match="must not set"):
            judge_rubric_coverage([_judge_spec("scenario_a")], classification_path=path)

    def test_criterion_missing_reason_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {"scenario_a": {"criteria": [{"criterion": "c", "class": "holistic"}]}})
        with pytest.raises(JudgeRubricClassificationError, match="reason"):
            judge_rubric_coverage([_judge_spec("scenario_a")], classification_path=path)

    def test_invalid_class_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {"scenario_a": _entry(criterion_class="vague")})
        with pytest.raises(JudgeRubricClassificationError, match="class"):
            judge_rubric_coverage([_judge_spec("scenario_a")], classification_path=path)

    def test_entry_with_empty_criteria_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        _write_classification(path, {"scenario_a": {"criteria": []}})
        with pytest.raises(JudgeRubricClassificationError, match="criteria"):
            judge_rubric_coverage([_judge_spec("scenario_a")], classification_path=path)


class TestClassificationCatalogIsFailLoud:
    """A missing/malformed classification file is a hard error, never a vacuous green."""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(JudgeRubricClassificationError, match="missing"):
            judge_rubric_coverage([], classification_path=tmp_path / "absent.yaml")

    def test_non_mapping_document_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "classification.yaml"
        path.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")
        with pytest.raises(JudgeRubricClassificationError, match="mapping"):
            judge_rubric_coverage([], classification_path=path)


class TestShippedJudgeRubricsAreFullyClassified:
    """Enforcing: every judge-bearing scenario in the real catalog is classified."""

    def test_no_shipped_judge_rubric_is_an_unclassified_gap(self) -> None:
        report = judge_rubric_coverage()
        assert report.gaps == (), (
            "judge-bearing scenario(s) ship with no entry in "
            f"{CLASSIFICATION_PATH} — classify each per evals/README.md § "
            "'Classifying a new judge rubric':\n  " + "\n  ".join(report.gaps)
        )

    def test_shipped_classification_carries_no_stale_entries(self) -> None:
        report = judge_rubric_coverage()
        assert report.stale == (), (
            "classification entry/entries name a scenario that is no longer "
            f"judge-bearing (renamed, deleted, or its judge block was dropped): {report.stale}"
        )
