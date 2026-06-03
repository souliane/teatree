r"""Tests for the shared whole-token term matcher (``teatree.hooks.term_match``).

Both configured term-list gates — the ``[teatree].banned_terms`` posting
gate (#1415) and the ``[overlay_leak].terms`` core-leak gate (BLUEPRINT § 1)
— share this matcher. It replaced a ``\b(term)\b`` regex that, once
loosened, surfaced a short term inside a longer run of the same alphabet
(a neutral example: a term ``acme`` matching inside ``acmecorp``).

Matching is WHOLE-TOKEN: text and term are both tokenized on any
non-alphanumeric character and a term matches only when its tokens appear as
a contiguous run of whole tokens, case-insensitively.

All term lists here are SYNTHETIC neutral fakes — no real customer/overlay
term value appears, so this public test file leaks nothing.
"""

import pytest

from teatree.hooks import term_match

# Synthetic term lists. ``op`` vs ``option`` mirrors the real operator-style
# false positive generically; ``acme``/``acme-corp``/``foo_bar`` exercise
# single-token, multi-token-kebab, and multi-token-snake terms together.
_ACME_TERMS = ("acme", "acme-corp", "foo_bar")


class TestTokens:
    def test_splits_on_every_non_alphanumeric_character(self) -> None:
        assert term_match.tokens("xx-acme, zz_qq") == ["xx", "acme", "zz", "qq"]

    def test_lowercases(self) -> None:
        assert term_match.tokens("Acme-PRODUCT") == ["acme", "product"]

    def test_pure_punctuation_yields_no_tokens(self) -> None:
        assert term_match.tokens("--- , .") == []


class TestSingleTokenTermMustBlock:
    @pytest.mark.parametrize(
        "text",
        ["acme", "x-acme-y", "ACME", "acme, hi", "deploy acme today", "acme.", "(acme)"],
    )
    def test_standalone_token_blocks(self, text: str) -> None:
        assert term_match.matched_term(text, _ACME_TERMS) == "acme"


class TestSingleTokenTermMustNotBlock:
    @pytest.mark.parametrize(
        "text",
        # The operator-style false positive, stated generically: a term that is
        # only a substring of one unbroken word never matches.
        ["acmecorp", "pacme", "acmeology", "foobar", "a normal sentence of words", ""],
    )
    def test_substring_inside_one_word_does_not_block(self, text: str) -> None:
        assert term_match.matched_term(text, _ACME_TERMS) is None

    def test_op_does_not_match_option(self) -> None:
        # The exact operator-class false positive: a short term ``op`` must not
        # surface inside ``option`` / ``operator``.
        assert term_match.matched_term("choose an option", ("op",)) is None
        assert term_match.matched_term("the operator pressed it", ("op",)) is None

    def test_op_matches_its_own_token(self) -> None:
        assert term_match.matched_term("set op to true", ("op",)) == "op"


class TestMultiTokenTerm:
    def test_kebab_term_matches_kebab_text(self) -> None:
        # Isolated kebab term: only ``acme-corp`` can match this line.
        assert term_match.matched_term("the acme-corp ships", ("acme-corp",)) == "acme-corp"

    def test_snake_term_matches_snake_and_space(self) -> None:
        assert term_match.matched_term("foo_bar value", ("foo_bar",)) == "foo_bar"
        assert term_match.matched_term("the foo bar value", ("foo_bar",)) == "foo_bar"

    def test_returns_first_configured_term_that_matches(self) -> None:
        # ``acme`` (a whole token of ``acme-corp``) appears first in the list,
        # so it is the one reported — the block decision is what matters, and
        # ``acme`` is genuinely present as a standalone token.
        assert term_match.matched_term("the acme-corp ships", _ACME_TERMS) == "acme"

    def test_kebab_and_space_tokenize_alike(self) -> None:
        # ``home-base`` and ``home base`` both tokenize to [home, base].
        assert term_match.matched_term("home base plan", ("home-base",)) == "home-base"
        assert term_match.matched_term("home-base plan", ("home base",)) == "home base"

    def test_multi_token_requires_contiguous_run(self) -> None:
        # A multi-token term only matches a contiguous run of its tokens.
        assert term_match.matched_term("foo then bar", _ACME_TERMS) is None

    def test_multi_token_does_not_match_a_single_token_subset(self) -> None:
        # ``acme-corp`` must not be reported merely because ``acme`` is present;
        # but ``acme`` (single-token term) legitimately does match.
        assert term_match.matched_term("acme alone", ("acme-corp",)) is None


class TestUnderscoreTermParity:
    """Underscore terms (the shape of some real entries) must keep working.

    They tokenize the same way as the text, so a synthetic ``widget_count`` →
    [widget, count] matches both ``widget_count`` and ``widget count``, while
    a glued single-token term matches only that whole token.
    """

    def test_snake_term_matches_underscore_and_space(self) -> None:
        assert term_match.matched_term("widget_count field", ("widget_count",)) == "widget_count"
        assert term_match.matched_term("a widget count here", ("widget_count",)) == "widget_count"

    def test_glued_single_token_matches_only_that_whole_token(self) -> None:
        assert term_match.matched_term("gluedsingletoken = []", ("gluedsingletoken",)) is not None
        assert term_match.matched_term("glued single token", ("gluedsingletoken",)) is None


class TestCaseInsensitive:
    def test_term_and_text_case_are_ignored(self) -> None:
        assert term_match.matched_term("ACME-Corp", ("acme-corp",)) == "acme-corp"
        assert term_match.matched_term("acme-corp", ("ACME-CORP",)) == "ACME-CORP"


class TestNoTermsConfigured:
    def test_empty_term_list_never_matches(self) -> None:
        assert term_match.matched_term("acme acme-corp foo_bar", ()) is None

    def test_pure_punctuation_term_never_matches(self) -> None:
        # A term that tokenizes to nothing cannot match anything.
        assert term_match.matched_term("anything at all", ("---",)) is None


class TestContainsRun:
    """The contiguous-sublist primitive directly (incl. the empty-needle guard)."""

    def test_empty_needle_never_matches(self) -> None:
        assert term_match._contains_run(["acme", "corp"], []) is False

    def test_single_token_needle_is_membership(self) -> None:
        assert term_match._contains_run(["a", "acme", "b"], ["acme"]) is True
        assert term_match._contains_run(["a", "b"], ["acme"]) is False

    def test_multi_token_needle_requires_contiguity(self) -> None:
        assert term_match._contains_run(["x", "acme", "corp", "y"], ["acme", "corp"]) is True
        assert term_match._contains_run(["acme", "x", "corp"], ["acme", "corp"]) is False


class TestLineMatches:
    def test_returns_true_on_a_whole_token_hit(self) -> None:
        assert term_match.line_matches("see x-acme-y here", _ACME_TERMS) is True

    def test_returns_false_on_a_substring_only_line(self) -> None:
        assert term_match.line_matches("acmecorp is a company", _ACME_TERMS) is False
