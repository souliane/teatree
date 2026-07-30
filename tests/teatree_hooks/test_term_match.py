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

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("acmeProduct", ["acme", "product"]),
            ("AcmeProduct", ["acme", "product"]),
            ("demoSavings", ["demo", "savings"]),
            ("DemoSavings", ["demo", "savings"]),
            ("getUserName", ["get", "user", "name"]),
            ("HTTPServer", ["http", "server"]),
            ("XMLParser", ["xml", "parser"]),
        ],
    )
    def test_splits_on_camelcase_and_acronym_boundaries(self, text: str, expected: list[str]) -> None:
        assert term_match.tokens(text) == expected

    def test_lowercase_glued_run_stays_one_token(self) -> None:
        # No case boundary, so nothing to split — stays a single token.
        assert term_match.tokens("demosavings") == ["demosavings"]


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
        # The operator-style false positive, stated generically: a SINGLE-word
        # term that is only a substring of one unbroken word never matches.
        # (Terms here are single-word only — a multi-word term legitimately
        # matches its glued spelling, covered in TestCamelCaseAndGluedMatching.)
        ["acmecorp", "pacme", "acmeology", "a normal sentence of words", ""],
    )
    def test_substring_inside_one_word_does_not_block(self, text: str) -> None:
        assert term_match.matched_term(text, ("acme",)) is None

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


class TestCamelCaseAndGluedMatching:
    """camelCase splitting + the multi-word glued fallback (the rework)."""

    @pytest.mark.parametrize("text", ["acmeProduct", "AcmeProduct", "acme-product", "xx-acme-zz", "acme"])
    def test_single_word_term_matches_camelcase_and_kebab(self, text: str) -> None:
        # The synthetic single-word term ``acme`` must match its whole token
        # however the surrounding identifier is glued.
        assert term_match.matched_term(text, ("acme",)) == "acme"

    @pytest.mark.parametrize(
        "clean",
        # The operator-class false positive, reproduced with the synthetic short
        # term ``op`` against generic English: it must NEVER surface inside a
        # longer unbroken word.
        ["operator", "operation", "operational", "cooperation", "opera", "operate", "option"],
    )
    def test_short_single_word_term_does_not_match_longer_unbroken_word(self, clean: str) -> None:
        assert term_match.matched_term(clean, ("op",)) is None

    @pytest.mark.parametrize("text", ["demoSavings", "DemoSavings", "demosavings", "demo-savings", "demo savings"])
    def test_multiword_term_matches_camelcase_pascal_and_glued(self, text: str) -> None:
        # A multi-word term matches kebab/space, the camelCase/Pascal split, and
        # the fully-lowercase glued spelling.
        assert term_match.matched_term(text, ("demo-savings",)) == "demo-savings"

    @pytest.mark.parametrize("text", ["acmeProduct", "AcmeProduct", "useAcmeClient"])
    def test_single_word_term_embedded_in_camelcase_matches(self, text: str) -> None:
        assert term_match.matched_term(text, ("acme",)) == "acme"

    def test_clean_camelcase_identifier_with_no_term_does_not_match(self) -> None:
        assert term_match.matched_term("getUserName", ("op", "demo-savings", "acme")) is None


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


class TestCompanyIdentifierAllowlistCarveOut:
    """The #1415 company-identifier carve-out (``allowlist`` parameter).

    A short banned term (``op`` here, the synthetic stand-in for the real org
    slug) must NOT surface inside a LONGER company-owned identifier that the
    operator allow-listed (``op-engineering`` / ``op-product``), nor inside an
    internal URL whose path carries those identifiers. A genuine customer
    codename NOT on the allow-list is unaffected — proving the carve-out does
    not gut the gate. All terms are SYNTHETIC neutral fakes.
    """

    _TERMS = ("op", "customercodename")
    _ALLOW = ("op-engineering", "op-product", "op-client-workspace")

    @pytest.mark.parametrize(
        "text",
        [
            "op-engineering",
            "op-product",
            "the op-engineering/op-product repo",
            "xx op-client-workspace yy",
            "https://gitlab.example/op-engineering/op-product/-/merge_requests/123",
            "see opEngineering and opProduct",  # camelCase company identifiers
        ],
    )
    def test_short_term_inside_allowlisted_identifier_is_not_flagged(self, text: str) -> None:
        # ``op`` is a sub-token of every allow-listed identifier here, so the
        # carve-out consumes the identifier and ``op`` never reaches matching.
        assert term_match.matched_term(text, self._TERMS, self._ALLOW) is None

    def test_internal_url_with_company_namespace_passes(self) -> None:
        url = "See https://gitlab.example/op-engineering/op-product/-/merge_requests/1 for details."
        assert term_match.matched_term(url, self._TERMS, self._ALLOW) is None

    def test_genuine_customer_codename_still_blocks_with_allowlist(self) -> None:
        # The control: a real customer codename NOT on the allow-list is still
        # blocked — the carve-out exempts only the company's own identifiers.
        text = "affects customercodename tenant"
        assert term_match.matched_term(text, self._TERMS, self._ALLOW) == "customercodename"

    def test_customer_codename_blocks_even_beside_allowlisted_identifier(self) -> None:
        text = "op-product change for the customercodename tenant"
        assert term_match.matched_term(text, self._TERMS, self._ALLOW) == "customercodename"

    def test_bare_short_term_not_part_of_identifier_still_matches(self) -> None:
        # A standalone ``op`` token NOT part of an allow-listed compound still
        # matches — the carve-out exempts the company's own COMPOUND identifiers,
        # not every appearance of the sub-token.
        assert term_match.matched_term("set op to true", self._TERMS, self._ALLOW) == "op"

    def test_no_allowlist_preserves_prior_behaviour(self) -> None:
        # With no allow-list, the short term surfaces inside the identifier
        # exactly as before — the carve-out is opt-in and inert by default.
        assert term_match.matched_term("op-engineering", self._TERMS) == "op"

    def test_empty_allowlist_entries_ignored(self) -> None:
        assert term_match.matched_term("op-engineering", self._TERMS, ("", "   ", "--")) == "op"

    def test_longer_identifier_consumed_as_one_run(self) -> None:
        # ``op-client-workspace`` (3 tokens) is consumed whole even though
        # ``op-product`` (2 tokens) shares the ``op`` prefix — longest-first.
        assert term_match.matched_term("op-client-workspace", self._TERMS, self._ALLOW) is None

    def test_line_matches_honours_allowlist(self) -> None:
        assert term_match.line_matches("op-engineering ships", self._TERMS, self._ALLOW) is False
        assert term_match.line_matches("op alone here", self._TERMS, self._ALLOW) is True


class TestStripAllowlistedTokens:
    """The token-run remover backing the carve-out, exercised directly."""

    def test_removes_a_multi_token_run(self) -> None:
        toks = term_match.tokens("the op engineering team")
        assert term_match._strip_allowlisted_tokens(toks, ("op-engineering",)) == ["the", "team"]

    def test_removes_a_single_token_entry(self) -> None:
        toks = term_match.tokens("the widget here")
        assert term_match._strip_allowlisted_tokens(toks, ("widget",)) == ["the", "here"]

    def test_keeps_a_partial_run(self) -> None:
        # Only ``op`` present, not the full ``op engineering`` run, so nothing
        # is consumed.
        toks = term_match.tokens("op then engineering later")
        assert term_match._strip_allowlisted_tokens(toks, ("op-engineering",)) == toks

    def test_empty_allowlist_is_identity(self) -> None:
        toks = term_match.tokens("op-engineering")
        assert term_match._strip_allowlisted_tokens(toks, ()) == toks


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
        # ``acme`` (single-word term) must not surface inside ``pacme``; the
        # glued multi-word match of ``acme-corp`` is intentional and covered
        # in TestCamelCaseAndGluedMatching, so it is excluded here.
        assert term_match.line_matches("pacme is a word", ("acme",)) is False


class TestIterTermMatches:
    """Position-tracked whole-token matches (consumed by the privacy gate)."""

    def test_empty_tokenizing_term_yields_nothing(self) -> None:
        assert term_match.iter_term_matches("anything here", "--") == []

    def test_no_hit_yields_empty(self) -> None:
        assert term_match.iter_term_matches("a clean sentence", "acme") == []

    def test_substring_only_yields_nothing(self) -> None:
        assert term_match.iter_term_matches("cooperative operation", "op") == []

    def test_single_token_yields_one_per_occurrence(self) -> None:
        matches = term_match.iter_term_matches("acme then acme again", "acme")
        assert [m[0] for m in matches] == ["acme", "acme"]
        assert matches[0][1] < matches[1][1]

    def test_camelcase_split_token_is_matched(self) -> None:
        matches = term_match.iter_term_matches("value = acmeClient", "acme")
        assert [m[0] for m in matches] == ["acme"]

    def test_multi_token_run_is_matched(self) -> None:
        matches = term_match.iter_term_matches("uses acme-corp here", "acme-corp")
        assert len(matches) == 1
        assert matches[0][0] == "acmecorp"

    def test_multi_token_glued_single_token_is_matched(self) -> None:
        matches = term_match.iter_term_matches("uses acmecorp here", "acme-corp")
        assert [m[0] for m in matches] == ["acmecorp"]


class TestBareSlugInCliShapedTextIsCaught:
    """A bare org-slug token in CLI-shaped text is a whole-token hit.

    ``t3 <slug> tasks ...`` tokenizes to ``[t3, <slug>, tasks, ...]`` — a space
    and a hyphen tokenize identically — so the bare ``<slug>`` is present and
    matchable. With an EMPTY allowlist it is caught. A ``t3-<slug>`` allowlist
    entry (tokens ``[t3, <slug>]``) is a contiguous run in that SAME CLI text,
    so the carve-out consumes the slug and it never reaches matching. ``op`` is
    the file's synthetic stand-in for an org slug.
    """

    _CLI_TEXT = "t3 op tasks work-next-headless"

    def test_empty_allowlist_catches_the_bare_slug(self) -> None:
        assert term_match.matched_term(self._CLI_TEXT, ("op",)) == "op"

    def test_t3_slug_allowlist_run_consumes_the_slug(self) -> None:
        assert term_match.matched_term(self._CLI_TEXT, ("op",), ("t3-op",)) is None
