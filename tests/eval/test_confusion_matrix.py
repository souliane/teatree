"""Confusion-matrix renderer over categorical audit outcomes (#2192)."""

import json

import pytest
from django.test import TestCase

from teatree.core.models import EvalVerdict, SessionAuditRecord
from teatree.eval.confusion_matrix import (
    ConfusionMatrix,
    build_confusion_matrix,
    from_records,
    render_confusion_json,
    render_confusion_text,
)


class TestBuildConfusionMatrix:
    def test_counts_and_diagonal(self) -> None:
        pairs = [
            ("backgrounded", "backgrounded"),
            ("backgrounded", "blocking"),
            ("blocking", "blocking"),
        ]
        matrix = build_confusion_matrix("backgrounding", pairs)
        assert matrix.axis == "backgrounding"
        assert matrix.count("backgrounded", "backgrounded") == 1
        assert matrix.count("backgrounded", "blocking") == 1
        assert matrix.count("blocking", "blocking") == 1
        assert matrix.count("blocking", "backgrounded") == 0

    def test_accuracy_is_diagonal_over_total(self) -> None:
        pairs = [
            ("a", "a"),
            ("a", "a"),
            ("a", "b"),
            ("b", "b"),
        ]
        matrix = build_confusion_matrix("ax", pairs)
        assert matrix.total == 4
        assert matrix.diagonal_total == 3
        assert matrix.accuracy == pytest.approx(0.75)

    def test_axes_are_not_symmetric(self) -> None:
        """A swap of expected/predicted must change the matrix — guards the axis order."""
        forward = build_confusion_matrix("ax", [("a", "b")])
        swapped = build_confusion_matrix("ax", [("b", "a")])
        assert forward.count("a", "b") == 1
        assert forward.count("b", "a") == 0
        assert swapped.count("a", "b") == 0
        assert swapped.count("b", "a") == 1

    def test_label_ordering_is_sorted_union(self) -> None:
        pairs = [
            ("zebra", "alpha"),
            ("mango", "mango"),
        ]
        matrix = build_confusion_matrix("ax", pairs)
        assert matrix.labels == ("alpha", "mango", "zebra")

    def test_extra_predicted_label_gets_a_column(self) -> None:
        """A predicted value never seen as expected still produces a column."""
        pairs = [("a", "a"), ("a", "off_axis")]
        matrix = build_confusion_matrix("ax", pairs)
        assert "off_axis" in matrix.labels
        assert matrix.count("a", "off_axis") == 1
        # off_axis never appears as an expected row, so its row total is 0.
        assert matrix.row_total("off_axis") == 0

    def test_row_total(self) -> None:
        pairs = [("a", "a"), ("a", "b"), ("a", "b"), ("b", "b")]
        matrix = build_confusion_matrix("ax", pairs)
        assert matrix.row_total("a") == 3
        assert matrix.row_total("b") == 1

    def test_off_diagonal_lists_failure_shapes(self) -> None:
        pairs = [("a", "a"), ("a", "b"), ("b", "a")]
        matrix = build_confusion_matrix("ax", pairs)
        shapes = matrix.off_diagonal()
        assert ("a", "b", 1) in shapes
        assert ("b", "a", 1) in shapes
        assert all(expected != predicted for expected, predicted, _ in shapes)

    def test_single_label_axis_is_perfect(self) -> None:
        matrix = build_confusion_matrix("ax", [("a", "a"), ("a", "a")])
        assert matrix.labels == ("a",)
        assert matrix.accuracy == pytest.approx(1.0)
        assert matrix.off_diagonal() == ()

    def test_empty_pairs_is_empty_matrix_accuracy_one(self) -> None:
        matrix = build_confusion_matrix("ax", [])
        assert matrix.labels == ()
        assert matrix.total == 0
        assert matrix.diagonal_total == 0
        # Vacuously perfect: no prediction was wrong.
        assert matrix.accuracy == pytest.approx(1.0)
        assert matrix.off_diagonal() == ()


class TestRenderConfusionText:
    def test_grid_has_labels_diagonal_marker_and_accuracy(self) -> None:
        pairs = [("a", "a"), ("a", "b"), ("b", "b")]
        matrix = build_confusion_matrix("backgrounding", pairs)
        text = render_confusion_text(matrix)
        assert "backgrounding" in text
        assert "a" in text
        assert "b" in text
        # Accuracy line: 2 correct of 3.
        assert "accuracy" in text.lower()
        assert "0.67" in text
        # Per-row totals appear.
        assert "total" in text.lower()

    def test_empty_matrix_renders_without_error(self) -> None:
        text = render_confusion_text(build_confusion_matrix("ax", []))
        assert "ax" in text
        assert "accuracy" in text.lower()


class TestRenderConfusionJson:
    def test_shape(self) -> None:
        pairs = [("a", "a"), ("a", "b"), ("b", "b")]
        matrix = build_confusion_matrix("backgrounding", pairs)
        payload = json.loads(render_confusion_json(matrix))
        assert payload["axis"] == "backgrounding"
        assert payload["labels"] == ["a", "b"]
        assert payload["total"] == 3
        assert payload["accuracy"] == round(2 / 3, 4)
        # counts is a row->col->n nested mapping.
        assert payload["counts"]["a"]["b"] == 1
        assert payload["counts"]["a"]["a"] == 1
        assert payload["counts"]["b"]["b"] == 1
        rows = {r["expected"]: r for r in payload["rows"]}
        assert rows["a"]["total"] == 2
        assert rows["b"]["total"] == 1

    def test_empty_json(self) -> None:
        payload = json.loads(render_confusion_json(build_confusion_matrix("ax", [])))
        assert payload["axis"] == "ax"
        assert payload["labels"] == []
        assert payload["total"] == 0
        assert payload["accuracy"] == pytest.approx(1.0)


class TestFromRecords(TestCase):
    def _record(self, expected: str, predicted: str, *, axis: str = "backgrounding", session: str) -> None:
        SessionAuditRecord.record(
            session_id=session,
            corpus_entry_id="entry",
            outcome_axis=axis,
            expected_outcome=expected,
            predicted_outcome=predicted,
            verdict=EvalVerdict.PASS if expected == predicted else EvalVerdict.FAIL,
            oracle="matcher",
        )

    def test_builds_from_queryset_on_one_axis(self) -> None:
        self._record("backgrounded", "backgrounded", session="s1")
        self._record("backgrounded", "blocking", session="s2")
        self._record("blocking", "blocking", session="s3")
        # A row on a different axis must be excluded.
        self._record("x", "y", axis="other_axis", session="s4")
        matrix = from_records("backgrounding", SessionAuditRecord.objects.all())
        assert matrix.total == 3
        assert matrix.count("backgrounded", "backgrounded") == 1
        assert matrix.count("backgrounded", "blocking") == 1
        assert matrix.count("blocking", "blocking") == 1
        assert matrix.accuracy == round(2 / 3, 4)

    def test_returns_confusion_matrix_instance(self) -> None:
        self._record("a", "a", session="s1")
        matrix = from_records("backgrounding", SessionAuditRecord.objects.all())
        assert isinstance(matrix, ConfusionMatrix)
