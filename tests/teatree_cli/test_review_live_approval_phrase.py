r"""Whole-word approval-phrase matcher rejects negated/embedded substrings (#1207).

Regression coverage for the substring false-positive class the original
``_has_approval_phrase`` implementation exposed:

* ``"don't post live"`` → previously matched ``"post live"`` substring
    → granted the live-publish approval. Now refused.
* ``"do NOT go ahead"`` → previously matched ``"go ahead"`` substring
    → granted the live-publish approval. Now refused.

The matcher is a minimal ``\b``-anchored regex per phrase; sentence-aware
NLP (negation scopes, polarity flips) is tracked as a class-C enforcement
follow-up.
"""

import pytest

from teatree.cli.review.live_approval import APPROVAL_PHRASES, _has_approval_phrase


class TestApprovalPhraseWholeWord:
    """``_has_approval_phrase`` matches whole-word, refuses negation substrings."""

    @pytest.mark.parametrize("phrase", APPROVAL_PHRASES)
    def test_bare_phrase_matches(self, phrase: str) -> None:
        assert _has_approval_phrase(phrase) is True

    @pytest.mark.parametrize("phrase", APPROVAL_PHRASES)
    def test_phrase_case_insensitive(self, phrase: str) -> None:
        assert _has_approval_phrase(phrase.upper()) is True
        assert _has_approval_phrase(phrase.title()) is True

    def test_please_go_ahead_matches(self) -> None:
        """``"please go ahead"`` is the canonical happy path — must match."""
        assert _has_approval_phrase("please go ahead") is True

    def test_go_ahead_and_post_it_matches(self) -> None:
        assert _has_approval_phrase("go ahead and post it") is True

    def test_negation_dont_post_live_does_not_match(self) -> None:
        """The substring matcher's primary false-positive — must be refused."""
        assert _has_approval_phrase("don't post live") is False

    def test_negation_do_not_post_live_does_not_match(self) -> None:
        assert _has_approval_phrase("do not post live") is False

    def test_negation_do_not_go_ahead_does_not_match(self) -> None:
        """The other substring false-positive named in the review."""
        assert _has_approval_phrase("do NOT go ahead") is False

    def test_negation_dont_submit_it_does_not_match(self) -> None:
        assert _has_approval_phrase("don't submit it") is False

    def test_embedded_inside_other_word_does_not_match(self) -> None:
        """A phrase embedded inside a longer word must not match."""
        assert _has_approval_phrase("foopost livebar") is False
        assert _has_approval_phrase("xgo aheady") is False

    def test_no_approval_phrase_present(self) -> None:
        assert _has_approval_phrase("thumbs up") is False
        assert _has_approval_phrase("looks good to me") is False
        assert _has_approval_phrase("") is False


class TestApprovalPhraseQuotation:
    """A quoted approval phrase is reported speech and must not authorize."""

    def test_double_quoted_phrase_does_not_match(self) -> None:
        assert _has_approval_phrase('the button is labelled "post it"') is False
        assert _has_approval_phrase('he said "go ahead" but I disagree') is False

    def test_curly_quoted_phrase_does_not_match(self) -> None:
        assert _has_approval_phrase("she wrote “approved” on the wrong ticket") is False

    def test_backtick_quoted_phrase_does_not_match(self) -> None:
        assert _has_approval_phrase("the CLI prints `ship it` on success") is False

    def test_unquoted_phrase_beside_a_quotation_still_matches(self) -> None:
        # The quotation is masked, but the real, unquoted approval still counts.
        assert _has_approval_phrase('he said "maybe later" but go ahead') is True

    def test_apostrophe_negation_still_refused_not_masked_as_a_quote(self) -> None:
        # Straight single quotes are NOT quote delimiters — the apostrophe in
        # "don't" must not be read as opening a quote span (which would swallow
        # the negation and wrongly grant approval).
        assert _has_approval_phrase("don't post live") is False
        assert _has_approval_phrase("please don't go ahead") is False
