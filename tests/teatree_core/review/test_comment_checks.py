"""The forge-neutral review-comment checks, composed for a published findings comment (#4968).

``review post-comment`` refuses a body that breaks the colleague prose cap, carries project
chatter, crams several file:line findings into one general note, or asserts an unbacked
"X is missing" claim. A published verdict comment is that same general note with no per-call
escapes, so each check here has a trigger body that withholds and a control body that passes.
"""

import pytest

from teatree.core.review.comment_checks import _count_paragraphs, _count_words, findings_comment_refusal


def _own() -> bool:
    return True


def _colleague() -> bool:
    return False


def _unreadable() -> None:
    return None


def _never_called() -> bool:
    pytest.fail("the PR author was read for a body under the prose cap")


_OVER_PARAGRAPHS = "a\n\nb\n\nc\n\nd"
_OVER_WORDS = "word " * 201


class TestColleagueProseCap:
    def test_an_over_cap_body_on_a_colleague_pr_is_refused_naming_the_breach(self) -> None:
        reason = findings_comment_refusal(_OVER_PARAGRAPHS, is_own_pr=_colleague)
        assert reason.startswith("colleague prose cap")
        assert "4-paragraph" in reason
        assert "3-paragraph cap" in reason

    def test_the_word_cap_is_named_when_the_paragraphs_fit(self) -> None:
        reason = findings_comment_refusal(_OVER_WORDS, is_own_pr=_colleague)
        assert "201-word" in reason
        assert "200-word cap" in reason

    def test_an_over_cap_body_on_the_owners_own_pr_passes(self) -> None:
        assert findings_comment_refusal(_OVER_PARAGRAPHS, is_own_pr=_own) == ""

    def test_an_unreadable_author_is_treated_as_a_colleague_pr_and_says_so(self) -> None:
        reason = findings_comment_refusal(_OVER_WORDS, is_own_pr=_unreadable)
        assert reason.startswith("colleague prose cap")
        assert "could not be read" in reason

    def test_a_body_under_the_cap_never_reads_the_author(self) -> None:
        assert findings_comment_refusal("one short finding", is_own_pr=_never_called) == ""


class TestCommentBloat:
    def test_a_stakeholder_handle_is_refused(self) -> None:
        reason = findings_comment_refusal("@bob flagged this loop", is_own_pr=_own)
        assert reason.startswith("comment bloat")

    def test_a_handle_inside_a_code_span_passes(self) -> None:
        assert findings_comment_refusal("the `@bob` fixture is unused", is_own_pr=_own) == ""


class TestMultiFindingGeneralNote:
    def test_two_distinct_file_line_cites_are_refused_with_their_count(self) -> None:
        reason = findings_comment_refusal("see `a.py:10` and `b.ts:3`", is_own_pr=_own)
        assert reason.startswith("multi-finding general note")
        assert "2 file:line findings" in reason

    def test_one_anchored_and_one_file_level_finding_pass(self) -> None:
        assert findings_comment_refusal("see `a.py:10` and `b.ts`", is_own_pr=_own) == ""


class TestUnbackedClaim:
    def test_a_missing_claim_is_refused_naming_the_phrase(self) -> None:
        reason = findings_comment_refusal("the retry helper is missing", is_own_pr=_own)
        assert reason.startswith("unbacked claim")
        assert "is missing" in reason

    def test_a_non_claim_observation_passes(self) -> None:
        assert findings_comment_refusal("rename the retry helper", is_own_pr=_own) == ""


class TestChainOrder:
    def test_the_prose_cap_is_checked_before_bloat(self) -> None:
        body = f"@bob said so\n\n{_OVER_PARAGRAPHS}"
        assert findings_comment_refusal(body, is_own_pr=_colleague).startswith("colleague prose cap")
        assert findings_comment_refusal(body, is_own_pr=_own).startswith("comment bloat")

    def test_bloat_is_checked_before_the_multi_finding_note(self) -> None:
        body = "@bob: a.py:1 and b.py:2"
        assert findings_comment_refusal(body, is_own_pr=_own).startswith("comment bloat")

    def test_the_multi_finding_note_is_checked_before_the_unbacked_claim(self) -> None:
        body = "a.py:1 is missing a guard and b.py:2 too"
        assert findings_comment_refusal(body, is_own_pr=_own).startswith("multi-finding general note")


class TestCounting:
    def test_count_paragraphs_splits_on_blank_lines(self) -> None:
        assert _count_paragraphs("") == 0
        assert _count_paragraphs("   ") == 0
        assert _count_paragraphs("just one paragraph") == 1
        assert _count_paragraphs("first\n\nsecond") == 2
        assert _count_paragraphs("a\n\nb\n\nc") == 3
        assert _count_paragraphs("a\n\n\n\nb") == 2
        assert _count_paragraphs("\n\na\n\n") == 1

    def test_count_words_splits_on_whitespace(self) -> None:
        assert _count_words("") == 0
        assert _count_words("   ") == 0
        assert _count_words("one") == 1
        assert _count_words("one two three") == 3
        assert _count_words("one\ntwo\tthree") == 3
