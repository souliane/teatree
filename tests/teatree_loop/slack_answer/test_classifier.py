"""Table-driven tests for the zero-token Slack-answer classifier (#1014).

``classify`` is pure logic — no DB, no network, no LLM. The fail-safe
contract is the load-bearing assertion: anything ambiguous routes to
``NEEDS_WORK`` so the cheap path never silently swallows a real request.
"""

import pytest

from teatree.loop.slack_answer.classifier import AnswerRoute, classify


class TestAckOnly:
    @pytest.mark.parametrize(
        "text",
        [
            "thanks",
            "Thanks!",
            "thank you",
            "ok",
            "okay",
            "got it",
            "👍",
            "lgtm",
            "LGTM 🙏",
            "perfect, thanks",
            "great",
            "cool 👍",
        ],
    )
    def test_short_acknowledgements_route_to_ack_only(self, text: str) -> None:
        assert classify(text) is AnswerRoute.ACK_ONLY

    def test_long_thanks_with_question_is_not_ack(self) -> None:
        # A "?" disqualifies ack even with a thanks token.
        assert classify("thanks — but what's the status?") is not AnswerRoute.ACK_ONLY

    def test_thanks_with_imperative_is_not_ack(self) -> None:
        assert classify("thanks, now fix the build") is AnswerRoute.NEEDS_WORK


class TestSimple:
    @pytest.mark.parametrize(
        "text",
        [
            "what's the status?",
            "what are you working on?",
            "which PRs are open?",
            "any blockers today?",
            "what's pending?",
            "status?",
            "what's the digest?",
            "what's blocking us today?",
        ],
    )
    def test_db_answerable_questions_route_to_simple(self, text: str) -> None:
        assert classify(text) is AnswerRoute.SIMPLE


class TestNeedsWork:
    @pytest.mark.parametrize(
        "text",
        [
            "fix the failing pipeline",
            "implement the new endpoint",
            "investigate why CI is red",
            "change the default timeout",
            "add a test for the parser",
            "can you look into the flaky test?",
            "why did the deploy fail?",  # investigation-needing
            "refactor the loop module",
            "please debug the scanner",
        ],
    )
    def test_imperatives_and_investigations_route_to_needs_work(self, text: str) -> None:
        assert classify(text) is AnswerRoute.NEEDS_WORK

    @pytest.mark.parametrize(
        "text",
        [
            "why is it broken?",
            "why is everything red?",
            "why was that wrong?",
        ],
    )
    def test_non_imperative_investigation_questions_route_to_needs_work(self, text: str) -> None:
        # No imperative verb, but the "why … broken/red/wrong" pattern
        # needs investigation — the classify-level investigation branch.
        assert classify(text) is AnswerRoute.NEEDS_WORK

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "hmm",
            "the thing about the other thing",
            "see attached",
            "🤔",
        ],
    )
    def test_ambiguous_input_fails_safe_to_needs_work(self, text: str) -> None:
        assert classify(text) is AnswerRoute.NEEDS_WORK
