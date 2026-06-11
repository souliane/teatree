"""Confusion-matrix value object and renderers over categorical audit outcomes.

The conversation-audit pass grades each captured session into a categorical
``(expected_outcome, predicted_outcome)`` pair on one ``outcome_axis``
(:class:`teatree.core.models.SessionAuditRecord`). A confusion matrix groups
those pairs into an expected x predicted grid: the diagonal is the correct
predictions, every off-diagonal cell is a *failure shape* (what the agent did
when it should have done something else). That is strictly richer than a
pass/fail tally — it names *how* the prediction was wrong, not just *that* it
was.

This module is the audit-domain sibling of :mod:`teatree.eval.report` (which
renders ``ScenarioResult`` for the eval-run domain) and :mod:`teatree.eval.matrix`
(the model x scenario grid). It lives in its own module because it operates on a
different domain object — keeping it out of ``report.py`` respects the
module-health bar and the single-responsibility shape those siblings already
follow.

:func:`build_confusion_matrix` is a pure function over ``(expected, predicted)``
pairs so the grid logic is unit-testable without the DB; :func:`from_records` is
a thin convenience that pulls the pairs off a :class:`SessionAuditRecord`
queryset via its ``.confusion_pairs`` manager method.
"""

import dataclasses
import json

from teatree.core.models.audit_run import SessionAuditQuerySet

#: Accuracy of an empty matrix: no prediction was wrong, so vacuously perfect.
_EMPTY_ACCURACY = 1.0
#: Rounding for the derived accuracy (matches the report.py float precision).
_ACCURACY_DIGITS = 4


@dataclasses.dataclass(frozen=True)
class ConfusionMatrix:
    """An expected x predicted count grid for one ``outcome_axis``.

    ``labels`` is the sorted union of every expected and predicted value, so a
    predicted value never seen as an expected one still gets a column. ``counts``
    maps an ``(expected, predicted)`` pair to the number of audited sessions with
    that outcome; a missing pair counts as zero.
    """

    axis: str
    labels: tuple[str, ...]
    counts: dict[tuple[str, str], int]

    def count(self, expected: str, predicted: str) -> int:
        """The number of sessions whose outcome was ``(expected, predicted)``."""
        return self.counts.get((expected, predicted), 0)

    def row_total(self, expected: str) -> int:
        """Total audited sessions whose *expected* outcome was ``expected``."""
        return sum(self.count(expected, predicted) for predicted in self.labels)

    @property
    def total(self) -> int:
        """Total audited sessions in the matrix."""
        return sum(self.counts.values())

    @property
    def diagonal_total(self) -> int:
        """Total correct predictions (the cells where expected == predicted)."""
        return sum(self.count(label, label) for label in self.labels)

    @property
    def accuracy(self) -> float:
        """Correct predictions over total, rounded; an empty matrix is ``1.0``."""
        if self.total == 0:
            return _EMPTY_ACCURACY
        return round(self.diagonal_total / self.total, _ACCURACY_DIGITS)

    def off_diagonal(self) -> tuple[tuple[str, str, int], ...]:
        """The failure shapes: ``(expected, predicted, count)`` for non-empty mismatches."""
        return tuple(
            (expected, predicted, n)
            for (expected, predicted), n in sorted(self.counts.items())
            if expected != predicted and n > 0
        )


def build_confusion_matrix(axis: str, pairs: list[tuple[str, str]]) -> ConfusionMatrix:
    """Build a :class:`ConfusionMatrix` from ``(expected, predicted)`` pairs.

    Pure: the same pairs always yield the same matrix. Labels are the sorted
    union of every expected and predicted value so the grid is deterministic and
    an off-axis predicted value still shows a column.
    """
    counts: dict[tuple[str, str], int] = {}
    seen: set[str] = set()
    for expected, predicted in pairs:
        counts[expected, predicted] = counts.get((expected, predicted), 0) + 1
        seen.add(expected)
        seen.add(predicted)
    return ConfusionMatrix(axis=axis, labels=tuple(sorted(seen)), counts=counts)


def from_records(axis: str, queryset: SessionAuditQuerySet) -> ConfusionMatrix:
    """Build a confusion matrix from a :class:`SessionAuditRecord` queryset.

    Thin convenience over :func:`build_confusion_matrix` — delegates pair
    extraction to the manager's ``.confusion_pairs`` so the DB query lives in one
    place (the model layer), not duplicated here.
    """
    return build_confusion_matrix(axis, queryset.confusion_pairs(axis))


def render_confusion_text(matrix: ConfusionMatrix) -> str:
    """Render an aligned expected x predicted grid with row totals and accuracy.

    Rows are expected outcomes, columns are predicted outcomes; the diagonal cell
    (a correct prediction) is suffixed with ``*``. Terse and deterministic, in the
    house style of :func:`teatree.eval.report.render_text`.
    """
    labels = matrix.labels
    row_header = f"axis={matrix.axis}  expected\\predicted"
    col_width = max(8, *(len(label) + 1 for label in labels)) if labels else 8
    name_width = max(len(row_header), *(len(label) for label in labels)) if labels else len(row_header)
    header = row_header.ljust(name_width) + "  " + "  ".join(label.rjust(col_width) for label in labels)
    if labels:
        header += "  " + "total".rjust(col_width)
    lines = [header, "-" * len(header)]
    for expected in labels:
        cells: list[str] = []
        for predicted in labels:
            n = matrix.count(expected, predicted)
            marked = f"{n}*" if expected == predicted else str(n)
            cells.append(marked.rjust(col_width))
        cells.append(str(matrix.row_total(expected)).rjust(col_width))
        lines.append(expected.ljust(name_width) + "  " + "  ".join(cells))
    lines.extend(("", f"accuracy: {matrix.accuracy:.2f} ({matrix.diagonal_total}/{matrix.total} correct)"))
    return "\n".join(lines)


def render_confusion_json(matrix: ConfusionMatrix) -> str:
    """Render the matrix as machine-readable JSON (deterministic key ordering)."""
    counts: dict[str, dict[str, int]] = {
        expected: {predicted: matrix.count(expected, predicted) for predicted in matrix.labels}
        for expected in matrix.labels
    }
    payload = {
        "axis": matrix.axis,
        "labels": list(matrix.labels),
        "total": matrix.total,
        "diagonal_total": matrix.diagonal_total,
        "accuracy": matrix.accuracy,
        "counts": counts,
        "rows": [
            {
                "expected": expected,
                "total": matrix.row_total(expected),
                "correct": matrix.count(expected, expected),
            }
            for expected in matrix.labels
        ],
        "off_diagonal": [
            {"expected": expected, "predicted": predicted, "count": n}
            for expected, predicted, n in matrix.off_diagonal()
        ],
    }
    return json.dumps(payload, indent=2)
