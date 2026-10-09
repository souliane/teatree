"""The forge-neutral review-comment checks, composed for a published findings review (#4968).

``review post-comment`` refuses a body that breaks the colleague prose cap, carries project
chatter, crams several file:line findings into one general note, or asserts an unbacked
"X is missing" claim. A published colleague review is held to the same checks with no per-call
escapes, so each check here has a trigger body that withholds and a control body that passes.
An inline comment is also held to the TODO anchor: a blocker next to the author's own TODO.
"""

from teatree.core.backend_protocols import PrReview, PrReviewComment
from teatree.core.review.comment_checks import _count_paragraphs, _count_words, comment_refusal, review_refusal

_OVER_PARAGRAPHS = "a\n\nb\n\nc\n\nd"
_OVER_WORDS = "word " * 201
_TWO_CITES = "see `a.py:10` and `b.ts:3`"
_DIFF = "@@ -9,0 +10,4 @@\n+def retry():\n+    pass\n+    # TODO: bound the retry loop\n+    return\n"


def _diffs() -> dict[str, str]:
    return {"a.py": _DIFF}


def _unread() -> dict[str, str]:
    msg = "the diff was read for a comment that does not read as a blocker"
    raise AssertionError(msg)


def _review(body: str = "", *comments: PrReviewComment) -> PrReview:
    return PrReview(commit_sha="c" * 40, body=body, comments=comments, marker="<!-- m -->")


class TestColleagueProseCap:
    def test_an_over_cap_body_is_refused_naming_the_breach(self) -> None:
        reason = comment_refusal(_OVER_PARAGRAPHS, general=True)
        assert reason.startswith("colleague prose cap")
        assert "4-paragraph" in reason
        assert "3-paragraph cap" in reason

    def test_the_word_cap_is_named_when_the_paragraphs_fit(self) -> None:
        reason = comment_refusal(_OVER_WORDS, general=False)
        assert "201-word" in reason
        assert "200-word cap" in reason


class TestCommentBloat:
    def test_a_stakeholder_handle_is_refused(self) -> None:
        assert comment_refusal("@bob flagged this loop", general=False).startswith("comment bloat")

    def test_a_handle_inside_a_code_span_passes(self) -> None:
        assert comment_refusal("the `@bob` fixture is unused", general=False) == ""


class TestMultiFindingGeneralNote:
    def test_two_distinct_file_line_cites_in_a_general_note_are_refused_with_their_count(self) -> None:
        reason = comment_refusal(_TWO_CITES, general=True)
        assert reason.startswith("multi-finding general note")
        assert "2 file:line findings" in reason

    def test_an_inline_comment_already_sits_on_its_line(self) -> None:
        assert comment_refusal(_TWO_CITES, general=False) == ""

    def test_one_anchored_and_one_file_level_finding_pass(self) -> None:
        assert comment_refusal("see `a.py:10` and `b.ts`", general=True) == ""


class TestUnbackedClaim:
    def test_a_missing_claim_is_refused_naming_the_phrase(self) -> None:
        reason = comment_refusal("the retry helper is missing", general=False)
        assert reason.startswith("unbacked claim")
        assert "is missing" in reason

    def test_a_non_claim_observation_passes(self) -> None:
        assert comment_refusal("rename the retry helper", general=False) == ""


class TestTodoAnchor:
    def test_a_blocker_next_to_the_authors_todo_is_refused_naming_the_marker(self) -> None:
        review = _review("", PrReviewComment(path="a.py", line=11, body="this loop must be bounded"))
        reason = review_refusal(review, file_diffs=_diffs)
        assert reason.startswith("the comment on a.py:11 — TODO-anchored blocker: line 12")
        assert "# TODO: bound the retry loop" in reason

    def test_a_comment_that_is_not_a_blocker_never_reads_the_diff(self) -> None:
        review = _review("", PrReviewComment(path="a.py", line=12, body="rename the retry helper"))
        assert review_refusal(review, file_diffs=_unread) == ""

    def test_a_blocker_outside_the_window_passes(self) -> None:
        review = _review("", PrReviewComment(path="a.py", line=16, body="this loop must be bounded"))
        assert review_refusal(review, file_diffs=_diffs) == ""

    def test_a_blocker_on_a_file_the_diff_does_not_carry_passes(self) -> None:
        review = _review("", PrReviewComment(path="b.py", line=12, body="this loop must be bounded"))
        assert review_refusal(review, file_diffs=_diffs) == ""

    def test_a_blocker_on_a_file_whose_patch_the_forge_omitted_is_refused(self) -> None:
        review = _review("", PrReviewComment(path="big.bin", line=3, body="this loop must be bounded"))
        reason = review_refusal(review, file_diffs=lambda: {"big.bin": None})
        assert reason.startswith("the comment on big.bin:3 — TODO anchor unreadable")


class TestChainOrder:
    def test_the_prose_cap_is_checked_before_bloat(self) -> None:
        assert comment_refusal(f"@bob said so\n\n{_OVER_PARAGRAPHS}", general=True).startswith("colleague prose cap")

    def test_bloat_is_checked_before_the_multi_finding_note(self) -> None:
        assert comment_refusal("@bob: a.py:1 and b.py:2", general=True).startswith("comment bloat")

    def test_the_multi_finding_note_is_checked_before_the_unbacked_claim(self) -> None:
        body = "a.py:1 is missing a guard and b.py:2 too"
        assert comment_refusal(body, general=True).startswith("multi-finding general note")

    def test_the_todo_anchor_is_checked_before_the_unbacked_claim(self) -> None:
        body = "the bound must be added, the guard is missing"
        review = _review("", PrReviewComment(path="a.py", line=12, body=body))
        assert "TODO-anchored blocker" in review_refusal(review, file_diffs=_diffs)


class TestReviewRefusal:
    def test_a_clean_review_passes(self) -> None:
        assert (
            review_refusal(
                _review("- rename x", PrReviewComment(path="a.py", line=9, body="rename y")), file_diffs=_diffs
            )
            == ""
        )

    def test_a_failing_comment_is_named_by_its_line(self) -> None:
        review = _review("", PrReviewComment(path="a.py", line=9, body="the retry helper is missing"))
        assert review_refusal(review, file_diffs=_diffs).startswith("the comment on a.py:9 — unbacked claim")

    def test_a_failing_body_is_named_as_the_summary(self) -> None:
        assert review_refusal(_review(_TWO_CITES), file_diffs=_unread).startswith(
            "the summary — multi-finding general note"
        )

    def test_the_multi_finding_check_skips_an_inline_comment(self) -> None:
        assert (
            review_refusal(_review("", PrReviewComment(path="a.py", line=9, body=_TWO_CITES)), file_diffs=_diffs) == ""
        )


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
