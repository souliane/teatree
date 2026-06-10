"""The anti-circular guard on matcher-graded corpus labels."""

import dataclasses

import pytest

from teatree.eval.corpus_grade import CircularOracleError, assert_independent_oracle
from teatree.eval.corpus_loader import discover_corpus
from teatree.eval.corpus_models import CorpusLabel


def _matcher_label() -> CorpusLabel:
    return next(label for label in discover_corpus() if label.oracle == "matcher")


def _judge_label() -> CorpusLabel:
    return next(label for label in discover_corpus() if label.oracle == "judge")


class TestAssertIndependentOracle:
    def test_human_labelled_by_passes(self) -> None:
        assert_independent_oracle(_matcher_label())

    def test_shipped_corpus_is_independent(self) -> None:
        for label in discover_corpus():
            assert_independent_oracle(label)

    def test_circular_matcher_label_raises(self) -> None:
        label = dataclasses.replace(_matcher_label(), labelled_by="skills/rules", rule_author="skills/rules")
        with pytest.raises(CircularOracleError, match="rule author"):
            assert_independent_oracle(label)

    def test_role_prefix_is_stripped_before_comparison(self) -> None:
        label = dataclasses.replace(_matcher_label(), labelled_by="agent:skills/rules", rule_author="skills/rules")
        with pytest.raises(CircularOracleError):
            assert_independent_oracle(label)

    def test_judge_oracle_with_same_author_passes(self) -> None:
        label = dataclasses.replace(_judge_label(), labelled_by="skills/code", rule_author="skills/code")
        assert_independent_oracle(label)

    def test_both_oracle_with_same_author_passes(self) -> None:
        label = dataclasses.replace(
            _matcher_label(), oracle="both", labelled_by="skills/rules", rule_author="skills/rules"
        )
        assert_independent_oracle(label)

    def test_empty_rule_author_passes(self) -> None:
        label = dataclasses.replace(_matcher_label(), labelled_by="skills/rules", rule_author="")
        assert_independent_oracle(label)

    def test_different_authors_pass(self) -> None:
        label = dataclasses.replace(_matcher_label(), labelled_by="agent:reviewer", rule_author="skills/rules")
        assert_independent_oracle(label)
