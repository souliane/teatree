"""Shared CLI ``print_table`` helper — alignment, non-TTY safety, empty state."""

import io

from teatree.core.table_output import print_table
from tests._ansi import strip_ansi


def _render(headers, rows, **kwargs) -> str:
    stream = io.StringIO()
    print_table(headers, rows, stream=stream, **kwargs)
    return strip_ansi(stream.getvalue())


class TestPrintTable:
    def test_renders_headers_and_rows(self) -> None:
        out = _render(["ID", "Name"], [["1", "Alice"], ["2", "Bob"]])
        assert "ID" in out
        assert "Name" in out
        assert "Alice" in out
        assert "Bob" in out

    def test_title_rendered_unwrapped(self) -> None:
        # A narrow table must not wrap the title (the reason it prints above the frame).
        out = _render(["A"], [["1"]], title="My Records")
        assert "My Records" in out

    def test_empty_rows_shows_no_rows(self) -> None:
        out = _render(["A", "B"], [])
        assert "(no rows)" in out

    def test_empty_rows_still_shows_title(self) -> None:
        out = _render(["A"], [], title="Nothing Here")
        assert "Nothing Here" in out

    def test_non_string_cells_coerced(self) -> None:
        out = _render(["N"], [[42]])
        assert "42" in out

    def test_piped_output_is_wide_enough_to_not_truncate(self) -> None:
        # A long value must render untruncated (no ellipsis) in a piped stream —
        # rich's 80-col non-TTY default would otherwise crush it.
        wide = "x" * 100
        out = _render(["Col"], [[wide]])
        assert wide in out

    def test_right_justify_column(self) -> None:
        out = _render(["ID"], [["7"]], justify=["right"])
        assert "7" in out
