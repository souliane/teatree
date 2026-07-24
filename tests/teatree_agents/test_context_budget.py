"""Tests for teatree.agents.context_budget — the E2BIG append byte budget."""

from teatree.agents.context_budget import MAX_APPEND_BYTES, enforce_budget


class TestEnforceBudget:
    def test_under_budget_is_byte_identical(self) -> None:
        text = "small context " + "x" * 100
        assert enforce_budget(text, [("x" * 100, "somewhere")], max_bytes=MAX_APPEND_BYTES) is text

    def test_truncates_first_block_first(self) -> None:
        big = "B" * 5000
        second = "S" * 5000
        text = f"head\n{big}\nmid\n{second}\ntail"
        out = enforce_budget(text, [(big, "block-one"), (second, "block-two")], max_bytes=6000)

        assert len(out.encode()) <= 6000
        # The first block absorbs the whole overage; the second stays intact.
        assert "…truncated" in out
        assert "see block-one" in out
        assert second in out

    def test_spills_to_second_block_when_first_insufficient(self) -> None:
        first = "A" * 2000
        second = "C" * 20000
        text = f"{first}\n{second}"
        out = enforce_budget(text, [(first, "first"), (second, "second")], max_bytes=4000)

        assert len(out.encode()) <= 4000
        assert "see first" in out
        assert "see second" in out

    def test_marker_reports_dropped_byte_count(self) -> None:
        block = "Z" * 10000
        out = enforce_budget(block, [(block, "the artifact")], max_bytes=1000)

        assert len(out.encode()) <= 1000
        assert "bytes; see the artifact" in out

    def test_multibyte_block_never_splits_a_codepoint(self) -> None:
        block = "é" * 10000  # 2 bytes each in UTF-8
        out = enforce_budget(block, [(block, "unicode")], max_bytes=1000)

        assert len(out.encode()) <= 1000
        out.encode()  # a split codepoint would already have raised on decode

    def test_empty_block_is_skipped(self) -> None:
        text = "H" * 5000
        # A missing (empty) block contributes nothing and must not crash.
        out = enforce_budget(text, [("", "absent"), (text, "present")], max_bytes=1000)
        assert len(out.encode()) <= 1000
        assert "see present" in out
