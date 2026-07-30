"""Slack table formatter — block schema, fence alignment/truncation, caps, empty."""

from typing import Any

from teatree.backends.slack.table_format import (
    MAX_COLS,
    MAX_DATA_ROWS,
    MAX_TOTAL_ROWS,
    TableMessage,
    render_table_message,
    slack_table_block,
    slack_table_fence,
)


# The formatter returns ``RawAPIDict`` (``dict[str, object]``); these ``Any``-typed
# accessors let the tests inspect the opaque Block Kit structure without a cast at
# every subscript.
def _cell_text(cell: Any) -> str:
    return cell["elements"][0]["elements"][0]["text"]


def _rows(block: Any) -> list[Any]:
    return list(block["rows"])


def _cols(block: Any) -> list[Any]:
    return list(block["column_settings"])


def _section_text(block: Any) -> str:
    return block["text"]["text"]


class TestSlackTableBlock:
    def test_block_type_is_table(self) -> None:
        block = slack_table_block(["A", "B"], [["1", "2"]])
        assert block["type"] == "table"

    def test_header_row_then_data_rows(self) -> None:
        block = slack_table_block(["A", "B"], [["1", "2"], ["3", "4"]])
        rows = _rows(block)
        assert [_cell_text(c) for c in rows[0]] == ["A", "B"]
        assert [_cell_text(c) for c in rows[1]] == ["1", "2"]
        assert [_cell_text(c) for c in rows[2]] == ["3", "4"]

    def test_header_cells_are_bold_data_cells_plain(self) -> None:
        block = slack_table_block(["A"], [["1"]])
        header_cell = _rows(block)[0][0]["elements"][0]["elements"][0]
        data_cell = _rows(block)[1][0]["elements"][0]["elements"][0]
        assert header_cell["style"] == {"bold": True}
        assert "style" not in data_cell

    def test_alignment_rides_column_settings(self) -> None:
        block = slack_table_block(["A", "B"], [["1", "2"]], alignment=["right", "center"])
        assert block["column_settings"] == [{"align": "right"}, {"align": "center"}]

    def test_alignment_defaults_to_left(self) -> None:
        block = slack_table_block(["A", "B"], [["1", "2"]])
        assert block["column_settings"] == [{"align": "left"}, {"align": "left"}]

    def test_rows_capped_at_max_total_rows(self) -> None:
        rows = [[str(i)] for i in range(MAX_TOTAL_ROWS + 50)]
        block = slack_table_block(["A"], rows)
        # Header counts toward Slack's 100-total table cap: 1 header + 99 data = 100.
        assert len(_rows(block)) == MAX_TOTAL_ROWS
        assert MAX_DATA_ROWS == MAX_TOTAL_ROWS - 1

    def test_full_table_never_exceeds_slack_100_row_total(self) -> None:
        # Slack rejects a ``table`` block with more than 100 rows total (the
        # header counts), hard-failing the whole DM. 150 input rows must yield
        # exactly 100 block rows — 1 header + 99 data — never 101.
        block = slack_table_block(["A"], [[str(i)] for i in range(150)])
        rows = _rows(block)
        assert len(rows) == 100
        assert [_cell_text(c) for c in rows[0]] == ["A"]  # header
        assert len(rows) - 1 == 99  # data rows

    def test_columns_capped_at_max_cols(self) -> None:
        headers = [f"c{i}" for i in range(MAX_COLS + 5)]
        block = slack_table_block(headers, [[str(i) for i in range(MAX_COLS + 5)]])
        assert len(_cols(block)) == MAX_COLS
        assert len(_rows(block)[0]) == MAX_COLS
        assert len(_rows(block)[1]) == MAX_COLS

    def test_ragged_row_padded_to_header_width(self) -> None:
        block = slack_table_block(["A", "B", "C"], [["only-one"]])
        assert [_cell_text(c) for c in _rows(block)[1]] == ["only-one", "", ""]

    def test_non_string_values_coerced(self) -> None:
        block = slack_table_block(["N"], [[42]])
        assert _cell_text(_rows(block)[1][0]) == "42"


class TestSlackTableFence:
    def test_wrapped_in_code_fence(self) -> None:
        fence = slack_table_fence(["A"], [["1"]])
        assert fence.startswith("```\n")
        assert fence.endswith("\n```")

    def test_empty_rows_render_no_rows(self) -> None:
        assert slack_table_fence(["A", "B"], []) == "```\n(no rows)\n```"

    def test_left_align_pads_right(self) -> None:
        fence = slack_table_fence(["Name"], [["Al"]], alignment=["left"])
        # "Al" left-justified into the "Name" (4) column width
        assert "Al  " in fence

    def test_right_align_pads_left(self) -> None:
        fence = slack_table_fence(["ID"], [["7"]], alignment=["right"])
        assert " 7" in fence

    def test_center_align_pads_both_sides(self) -> None:
        fence = slack_table_fence(["ABCDE"], [["x"]], alignment=["center"])
        assert "  x  " in fence

    def test_single_char_column_truncates_to_bare_ellipsis(self) -> None:
        # A tight budget shrinks both columns to width 1; an over-wide cell then
        # truncates to the bare ellipsis rather than raising.
        fence = slack_table_fence(["A", "B"], [["xxxx", "yyyy"]], max_width=5)
        assert "…" in fence

    def test_separator_rule_present(self) -> None:
        fence = slack_table_fence(["A", "B"], [["1", "2"]])
        lines = fence.splitlines()
        # line 0 is ```, line 1 header, line 2 the rule row
        assert set(lines[2]) <= {"-", "+"}

    def test_widest_column_truncated_first_to_fit_max_width(self) -> None:
        headers = ["short", "verylongcolumnheader"]
        rows = [["x", "y" * 40]]
        fence = slack_table_fence(headers, rows, max_width=20)
        body_lines = [line for line in fence.splitlines() if line != "```"]
        # every rendered line fits the budget, and the wide column is ellipsised
        assert all(len(line) <= 20 for line in body_lines)
        assert "…" in fence

    def test_no_truncation_when_within_budget(self) -> None:
        fence = slack_table_fence(["A", "B"], [["1", "2"]], max_width=72)
        assert "…" not in fence

    def test_never_wraps_a_cell(self) -> None:
        fence = slack_table_fence(["Col"], [["a very long single cell value here"]], max_width=12)
        data_lines = [line for line in fence.splitlines() if line != "```"]
        # header + rule + exactly one data line — the long cell is truncated, not wrapped
        assert len(data_lines) == 3

    def test_rows_capped_with_and_more_trailer(self) -> None:
        fence = slack_table_fence(["A"], [[str(i)] for i in range(150)])
        lines = [line for line in fence.splitlines() if line != "```"]
        # header + rule + MAX_DATA_ROWS data rows + one honest trailer
        assert lines[-1] == f"… and {150 - MAX_DATA_ROWS} more"
        assert len(lines[2:-1]) == MAX_DATA_ROWS

    def test_no_trailer_when_within_cap(self) -> None:
        fence = slack_table_fence(["A"], [["1"], ["2"]])
        assert "more" not in fence


class TestRenderTableMessage:
    def test_returns_blocks_and_fence(self) -> None:
        msg = render_table_message(["A"], [["1"]])
        assert isinstance(msg, TableMessage)
        assert msg.blocks[0]["type"] == "table"
        assert msg.fence.startswith("```\n")

    def test_title_prepends_section_block_and_fence_line(self) -> None:
        msg = render_table_message(["A"], [["1"]], title="My Table")
        assert msg.blocks[0]["type"] == "section"
        assert _section_text(msg.blocks[0]) == "*My Table*"
        assert msg.blocks[1]["type"] == "table"
        assert msg.fence.startswith("*My Table*\n```")

    def test_no_title_omits_section_block(self) -> None:
        msg = render_table_message(["A"], [["1"]])
        assert [b["type"] for b in msg.blocks] == ["table"]

    def test_empty_rows_message_still_has_table_block_and_no_rows_fence(self) -> None:
        msg = render_table_message(["A"], [])
        assert msg.blocks[0]["type"] == "table"
        assert "(no rows)" in msg.fence
