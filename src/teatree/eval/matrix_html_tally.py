"""Read the per-model tally back out of a rendered benchmark matrix dashboard.

:func:`~teatree.eval.matrix.render_matrix_html` is the writer; this is its reader
counterpart. The weekly workflow's publish job only ever holds the rendered HTML
artifacts (the shard jobs upload nothing else), so the tally table is the one
substrate a publish-time guard can read. Parsing is structural — the tally table
is located by its header row, not by position — and a document carrying no such
table is a fail-loud :class:`MatrixTallyError` rather than a silent empty list.
"""

import dataclasses
from html.parser import HTMLParser

TALLY_HEADER = ("model", "passed", "failed", "skipped", "errored", "cost")
#: The renderer formats cost as ``$%.4f``, so anything below this is indistinguishable
#: from ``$0.0000`` in the artifact and reads as unmetered.
RENDERED_COST_RESOLUTION_USD = 0.0001


class MatrixTallyError(RuntimeError):
    """The HTML carries no ``render_matrix_html`` per-model tally table."""


@dataclasses.dataclass(frozen=True)
class ModelTally:
    """One model's footer row: verdict counts plus the total metered spend."""

    model: str
    passed: int
    failed: int
    skipped: int
    errored: int
    cost_usd: float

    @property
    def verdicts(self) -> int:
        """Graded pass/FAIL cells. ``skipped`` was never run; ``errored`` is not graded."""
        return self.passed + self.failed

    @property
    def is_unmetered(self) -> bool:
        """Graded verdicts recorded against zero metered spend — the exhausted-window signature."""
        return self.verdicts > 0 and self.cost_usd < RENDERED_COST_RESOLUTION_USD


class _TableRowExtractor(HTMLParser):
    """Collect every ``<tr>`` in the document as a list of its cell texts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def parse_model_tallies(html_text: str) -> list[ModelTally]:
    """Parse the per-model tally rows out of a rendered matrix dashboard."""
    extractor = _TableRowExtractor()
    extractor.feed(html_text)
    rows = extractor.rows
    header_index = next((i for i, row in enumerate(rows) if tuple(row) == TALLY_HEADER), None)
    if header_index is None:
        msg = f"no per-model tally table (expected a header row {TALLY_HEADER})"
        raise MatrixTallyError(msg)
    return [_parse_tally_row(row) for row in rows[header_index + 1 :] if len(row) == len(TALLY_HEADER)]


def _parse_tally_row(row: list[str]) -> ModelTally:
    model, passed, failed, skipped, errored, cost = row
    try:
        return ModelTally(
            model=model,
            passed=int(passed),
            failed=int(failed),
            skipped=int(skipped),
            errored=int(errored),
            cost_usd=float(cost.lstrip("$")),
        )
    except ValueError as exc:
        msg = f"malformed tally row {row!r}: {exc}"
        raise MatrixTallyError(msg) from exc
