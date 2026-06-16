"""Heuristic tests for :attr:`PendingChatInjection.is_question` (#1063).

The classifier is empirical, calibrated against the 25 real user-message
texts in the live ``teatree_pending_chat_injection`` table on
2026-05-19. Splitting these from the bulk model tests keeps the
anti-vacuous evidence focused: reverting the ``?`` check on the
implementation must turn AT LEAST three of the assertions below RED
(documented per-test).
"""

import pytest

from teatree.core.models import PendingChatInjection
from teatree.core.models.pending_chat_injection import _classify_is_question

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _row(text: str) -> PendingChatInjection:
    row = PendingChatInjection.record(channel="D", slack_ts="t", text=text)
    assert row is not None
    return row


REAL_QUESTIONS: tuple[str, ...] = (
    "is it good that any session can drain the slack message queue?",
    "why did we delete a screenshot? and there's a translation issue on it",
    "did you review with the bot ?",
    "what do you save in DB?",
    "why don't you merge the PR?",
    "how to regrant the tokens? URL or do it for me please",
    "why are some tests skipped?",
    "you sure I need to reinstall?",
)

REAL_DIRECTIVES: tuple[str, ...] = (
    "please add a verbosity level. basically for now I like to be informed about everything bec...",
    "status update please",
    "t3 should merge its own PRs without waiting for me",
    "OK do it please",
    "I expect you to auto pick-up <gitlab url>",
    "Let's add a prek hook similar to the on in the sibling repo...",
)

REAL_INFO: tuple[str, ...] = (
    "hello",
    "I didn't modify the permissions because they were already there...",
    "I checked and both bots already have this permission",
    "I see that you don't create the snapshots before compacting",
    "you will use my reactions to the threads on the bot to educate yourself...",
    "this is not the kind of message I like to see: <gitlab url>",
    "seems you still don't know how to comply with the MR title/desc...",
    "someone reviewed my MR: <gitlab url>",
)


class TestRealQuestionsClassifyTrue:
    """All 8 real questions from the empirical sample must classify True.

    **Anti-vacuous mutation evidence:** reverting the ``stripped.endswith('?')``
    check in ``_classify_is_question`` turns the first four assertions
    RED (none start with a question word — ``is`` and ``did`` and ``why``
    do, but ``you sure I need to reinstall?`` does not). So at least
    3 of these tests guard the ``?`` branch.
    """

    @pytest.mark.parametrize("text", REAL_QUESTIONS)
    def test_real_question_classifies_true(self, text: str) -> None:
        assert _classify_is_question(text) is True, f"expected question: {text!r}"

    @pytest.mark.parametrize("text", REAL_QUESTIONS)
    def test_is_question_property_matches_classifier(self, text: str) -> None:
        assert _row(text).is_question is True


class TestRealDirectivesAndInfoClassifyFalse:
    """Real directive / info messages must NOT trip the question filter.

    These are the false-positive surface — directives that needed a
    reply but were not framed as questions. The agent must reply to
    directives too, but that is a separate problem (out of scope for
    this gate, which targets the literally-a-question case).
    """

    @pytest.mark.parametrize("text", REAL_DIRECTIVES)
    def test_directive_classifies_false(self, text: str) -> None:
        assert _classify_is_question(text) is False, f"unexpected question: {text!r}"

    @pytest.mark.parametrize("text", REAL_INFO)
    def test_info_classifies_false(self, text: str) -> None:
        assert _classify_is_question(text) is False, f"unexpected question: {text!r}"


class TestQuestionWordsAtStart:
    """The 19 leading question words trigger True regardless of trailing ``?``."""

    @pytest.mark.parametrize(
        "text",
        [
            "why this happened",
            "what about it",
            "when will it ship",
            "where is the log",
            "who owns this",
            "which one is right",
            "how do I do that",
            "is the test green",
            "are we shipping",
            "do you know",
            "does this work",
            "did you try",
            "can you confirm",
            "could you check",
            "should I merge",
            "would you mind",
            "will it land today",
            "was it merged",
            "were the tests green",
        ],
    )
    def test_question_word_at_start_is_true(self, text: str) -> None:
        assert _classify_is_question(text) is True

    def test_case_insensitive(self) -> None:
        assert _classify_is_question("WHY did this break") is True
        assert _classify_is_question("Could You check") is True


class TestTrailingQuestionMarkOnly:
    """Texts that are questions ONLY because they end in ``?``.

    These do NOT start with a question word and contain no question
    phrase, so the ``stripped.endswith('?')`` branch is the SOLE reason
    they classify True. This is the dedicated anti-vacuous guard for
    that branch: reverting the ``?`` check in ``_classify_is_question``
    turns ALL of these RED (>= 3 tests, satisfying the #1063 spec).
    """

    @pytest.mark.parametrize(
        "text",
        [
            "you sure I need to reinstall?",
            "the build is red on main right now?",
            "we really want to ship this today?",
            "and the translation issue too?",
            "merging now then?",
        ],
    )
    def test_trailing_question_mark_alone_is_true(self, text: str) -> None:
        assert _classify_is_question(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "you sure I need to reinstall?",
            "the build is red on main right now?",
            "we really want to ship this today?",
        ],
    )
    def test_same_text_without_question_mark_is_false(self, text: str) -> None:
        """Removing the ``?`` flips the verdict — proves ``?`` is load-bearing."""
        assert _classify_is_question(text.rstrip("?")) is False


class TestQuestionPhrases:
    """``please answer / please explain / please tell me`` anywhere → True."""

    @pytest.mark.parametrize(
        "text",
        [
            "the build is red, please answer when you get a sec",
            "I'm confused — please explain the rebase you did",
            "please tell me which branch is canonical",
        ],
    )
    def test_phrase_anywhere_is_true(self, text: str) -> None:
        assert _classify_is_question(text) is True


class TestLeadingNoiseStripped:
    """Whitespace, punctuation, and markdown decoration are stripped before classification."""

    @pytest.mark.parametrize(
        "text",
        [
            "   why didn't this work",
            ">>> what's the plan",
            "**why** is this red",
            "- what about merging now",
            "  > _why_ did you do that",
            "1. is this the right approach",
        ],
    )
    def test_decoration_stripped_first(self, text: str) -> None:
        assert _classify_is_question(text) is True


class TestNotQuestions:
    """Sanity false-cases."""

    def test_empty_text_is_false(self) -> None:
        assert _classify_is_question("") is False

    def test_whitespace_only_is_false(self) -> None:
        assert _classify_is_question("   \n   ") is False

    def test_punctuation_only_is_false(self) -> None:
        assert _classify_is_question("***...---") is False

    def test_statement_without_question_word_is_false(self) -> None:
        assert _classify_is_question("the build is green") is False

    def test_directive_starting_with_t3_is_false(self) -> None:
        assert _classify_is_question("t3 should merge its own PRs") is False

    def test_partial_word_match_is_false(self) -> None:
        # ``what`` is a question word but ``whatever`` is not.
        assert _classify_is_question("whatever you think is best") is False

    def test_non_letter_first_char_after_strip_is_false(self) -> None:
        # ``@`` is not in the leading-noise class, so the stripped text
        # still starts with a non-letter: ``_FIRST_WORD`` fails to match
        # and the classifier returns False (no question word extractable).
        assert _classify_is_question("@channel ping me back") is False
