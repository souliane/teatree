"""Tests for ``teatree.utils.close_keywords`` — the shared Closes/Fixes footer parser."""

from teatree.utils.close_keywords import parse_closes_ticket


class TestParseClosesTicket:
    def test_matches_closes_hash_n(self) -> None:
        assert parse_closes_ticket("Closes #855") == "855"

    def test_matches_fixes_hash_n(self) -> None:
        assert parse_closes_ticket("Fixes #856 — broken thing") == "856"

    def test_matches_resolves_hash_n(self) -> None:
        assert parse_closes_ticket("This MR resolves #99") == "99"

    def test_matches_case_insensitive(self) -> None:
        assert parse_closes_ticket("CLOSES #1") == "1"

    def test_matches_with_colon(self) -> None:
        assert parse_closes_ticket("Closes: #42") == "42"

    def test_returns_empty_when_no_keyword(self) -> None:
        assert parse_closes_ticket("Related to #99") == ""

    def test_returns_empty_when_no_hash(self) -> None:
        assert parse_closes_ticket("Closes nothing in particular") == ""

    def test_returns_first_match_only(self) -> None:
        assert parse_closes_ticket("Closes #1\nFixes #2") == "1"

    def test_returns_empty_on_empty_description(self) -> None:
        assert parse_closes_ticket("") == ""

    def test_word_boundary_rejects_glued_prefix(self) -> None:
        assert parse_closes_ticket("preCloses #7") == ""
