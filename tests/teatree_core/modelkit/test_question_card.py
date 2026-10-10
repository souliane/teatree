"""The owner-question card: what it must carry, what it refuses, and how it reads as plain text (#4990)."""

import dataclasses
import json
from collections.abc import Callable

import pytest

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import (
    MAX_QUESTION_CHARS,
    MAX_WORDS,
    CardOption,
    QuestionCard,
    internal_shorthand,
    plain_text,
    word_count,
)

SAMPLE_QUESTION = "Can you mention the bot once (type @ and its name) in any Slack channel?"
SAMPLE_WHY = "This tells me whether it is set up for channels or only for our direct messages."
SAMPLE_TEXT = """\
Can you mention the bot once (type @ and its name) in any Slack channel?
This tells me whether it is set up for channels or only for our direct messages.

[Done] (recommended) - I check which setup is installed.
[Later] - I ask you again later.

Or reply in this thread.
ref: question 42"""

SAMPLE_CARD = QuestionCard(
    decision=OwnerDecision.PRODUCT_SCOPE,
    checked=("The bot answers direct messages.",),
    blocker="Only the owner can say which setup is wanted.",
    why=SAMPLE_WHY,
    options=(
        CardOption("Done", "I check which setup is installed.", recommended=True),
        CardOption("Later", "I ask you again later."),
    ),
)


def _with_quote_of_words(card: QuestionCard, question: str, total: int) -> QuestionCard:
    base = word_count(plain_text(dataclasses.replace(card, quoted="x").view(question, ref=1))) - 1
    return dataclasses.replace(card, quoted=" ".join(["word"] * (total - base)))


def _options(*labels: str, recommended: int | None = 0) -> tuple[CardOption, ...]:
    return tuple(
        CardOption(label, "I carry on.", recommended=index == recommended) for index, label in enumerate(labels)
    )


class TestTheApprovedSample:
    def test_the_approved_sample_counts_49_words(self) -> None:
        text = plain_text(SAMPLE_CARD.view(SAMPLE_QUESTION, ref=42))

        assert text == SAMPLE_TEXT
        assert word_count(text) == 49

    def test_a_link_counts_as_the_words_the_owner_reads(self) -> None:
        labelled = "Can you read <https://git.acme.example/widgets/pulls/7|this pull request>?"

        assert word_count(labelled) == word_count("Can you read this pull request?") == 6
        assert word_count("Can you read https://git.acme.example/widgets/pulls/7 today?") == 5

    def test_the_ref_line_is_not_counted(self) -> None:
        assert word_count("Is it fine?\nref: question 7") == word_count("Is it fine?") == 3

    def test_the_sample_card_has_no_problems(self) -> None:
        assert SAMPLE_CARD.problems(SAMPLE_QUESTION) == []

    def test_the_card_is_capped_at_120_words(self) -> None:
        at_cap = _with_quote_of_words(SAMPLE_CARD, SAMPLE_QUESTION, MAX_WORDS)
        over_cap = _with_quote_of_words(SAMPLE_CARD, SAMPLE_QUESTION, MAX_WORDS + 1)

        assert word_count(plain_text(at_cap.view(SAMPLE_QUESTION, ref=1))) == MAX_WORDS
        assert at_cap.problems(SAMPLE_QUESTION) == []
        [problem] = over_cap.problems(SAMPLE_QUESTION)
        assert "121 words" in problem
        assert "120" in problem


class TestTheLayout:
    def test_a_card_without_options_asks_for_a_reply_in_the_thread(self) -> None:
        card = dataclasses.replace(SAMPLE_CARD, options=())

        text = plain_text(card.view(SAMPLE_QUESTION, ref=3))

        assert text.splitlines()[-2:] == ["Reply in this thread.", "ref: question 3"]
        assert "[" not in text

    def test_a_quote_sits_between_the_why_and_the_options(self) -> None:
        card = dataclasses.replace(SAMPLE_CARD, quoted="Thanks, see you soon.")

        lines = plain_text(card.view(SAMPLE_QUESTION, ref=3)).splitlines()

        assert lines[2:5] == ["", "> Thanks, see you soon.", ""]
        assert "" not in lines[:2] + lines[5:7]

    def test_an_option_without_a_description_renders_its_label_alone(self) -> None:
        card = dataclasses.replace(SAMPLE_CARD, options=(CardOption("Yes", ""), CardOption("No", "")))

        assert "[Yes]\n[No]\n" in plain_text(card.view(SAMPLE_QUESTION, ref=3))


_Mutate = Callable[[QuestionCard], QuestionCard]


def _set(**changes: object) -> _Mutate:
    return lambda card: dataclasses.replace(card, **changes)


_SHORTHAND_FIELDS: dict[str, Callable[[str], _Mutate]] = {
    "why": lambda token: _set(why=f"This is about {token} today."),
    "quote": lambda token: _set(quoted=f"Please read {token} first."),
    "checked fact": lambda token: _set(checked=(f"I read {token} today.",)),
    "blocker": lambda token: _set(blocker=f"Only the owner knows {token}."),
    "option label": lambda token: _set(options=_options(f"Use {token}", "Later")),
    "option consequence": lambda token: _set(
        options=(CardOption("Go", f"I use {token}.", recommended=True), CardOption("Wait", "I wait."))
    ),
}
_SHORTHAND_TOKENS = [
    "#42",
    "rule 1",
    "widget_count",
    "acme_only",
    "101, 102, 103",
    "src/teatree/core/notify.py",
    "`t3 run`",
    "--force",
    "mode=fast",
    "deferred",
]


class TestCardProblems:
    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            pytest.param(_set(blocker=""), "blocker", id="no blocker"),
            pytest.param(_set(why=""), "why", id="no why"),
            pytest.param(_set(why="It matters. It really does."), "why", id="two why sentences"),
            pytest.param(_set(checked=()), "checked", id="no checked fact"),
            pytest.param(_set(options=_options("Only")), "option", id="one option"),
            pytest.param(
                _set(options=(CardOption("A", "", recommended=True), CardOption("B", "y"))),
                "consequence",
                id="no consequence",
            ),
            pytest.param(_set(options=_options("A", "B", "C", "D", "E")), "option", id="five options"),
            pytest.param(_set(options=_options("Same", "Same")), "duplicate", id="duplicate labels"),
            pytest.param(_set(options=_options("A", "B", recommended=None)), "recommended", id="no recommended"),
            pytest.param(
                _set(options=(CardOption("A", "x", recommended=True), CardOption("B", "y", recommended=True))),
                "recommended",
                id="two recommended",
            ),
            pytest.param(_set(options=_options("A", "B", recommended=1)), "first", id="not first"),
            pytest.param(_set(blocker="b" * 201), "blocker", id="blocker over cap"),
            pytest.param(_set(why="w" * 201 + "."), "why", id="why over cap"),
            pytest.param(
                _set(options=(CardOption("L" * 41, "x", recommended=True), CardOption("B", "y"))),
                "label",
                id="label over cap",
            ),
            pytest.param(
                _set(options=(CardOption("A", "d" * 161, recommended=True), CardOption("B", "y"))),
                "consequence",
                id="consequence over cap",
            ),
        ],
    )
    def test_card_problems(self, mutate: _Mutate, expected: str) -> None:
        problems = mutate(SAMPLE_CARD).problems(SAMPLE_QUESTION)

        assert problems
        assert any(expected in problem for problem in problems), problems

    @pytest.mark.parametrize(
        "question",
        ["Can you mention the bot", "Can you? Or not?", "q" * 161 + "?", "Can you\nmention the bot?"],
        ids=["no question mark", "two sentences", "over cap", "two lines"],
    )
    def test_the_question_must_be_one_short_sentence_ending_in_a_question_mark(self, question: str) -> None:
        assert any("question" in problem for problem in SAMPLE_CARD.problems(question))

    def test_a_link_counts_as_the_label_the_owner_reads(self) -> None:
        url = "https://git.acme.example/group/subgroup/nested/project/-/merge_requests/123456/diffs?view=inline"
        question = f"Can I post the review request for <{url}|this pull request> without an independent review?"

        assert len(question) > MAX_QUESTION_CHARS
        assert SAMPLE_CARD.problems(question) == []

    def test_a_question_that_reads_over_the_cap_is_refused_whatever_its_links(self) -> None:
        question = f"Can I post <https://git.acme.example/x|{'q' * 150}> today?"

        problems = SAMPLE_CARD.problems(question)

        assert any("the question is 168 characters; at most 160" in problem for problem in problems), problems

    @pytest.mark.parametrize("token", _SHORTHAND_TOKENS)
    @pytest.mark.parametrize("field", sorted(_SHORTHAND_FIELDS))
    def test_internal_shorthand_is_refused_wherever_the_owner_or_the_reader_sees_it(
        self, field: str, token: str
    ) -> None:
        problems = _SHORTHAND_FIELDS[field](token)(SAMPLE_CARD).problems(SAMPLE_QUESTION)

        assert any(token.strip("`") in problem for problem in problems), problems

    @pytest.mark.parametrize("token", _SHORTHAND_TOKENS)
    def test_internal_shorthand_in_the_question_is_refused(self, token: str) -> None:
        problems = SAMPLE_CARD.problems(f"Can you open {token} today?")

        assert any(token.strip("`") in problem for problem in problems), problems

    @pytest.mark.parametrize("text", ["Is this and/or that fine?", "Is 24/7 fine?", "Does CI/CD pass, e.g. today?"])
    def test_everyday_slashes_and_abbreviations_pass(self, text: str) -> None:
        assert SAMPLE_CARD.problems(text) == []

    def test_every_problem_is_listed_at_once(self) -> None:
        card = dataclasses.replace(SAMPLE_CARD, blocker="", why="")

        problems = card.problems("Can you open #42")

        assert len(problems) >= 4


class TestInternalShorthand:
    @pytest.mark.parametrize(
        ("text", "tokens"),
        [
            ("see #105 now", ["#105"]),
            ("see !7 now", ["!7"]),
            ("see ticket 104 now", ["ticket 104"]),
            ("see Pull Request 9 now", ["Pull Request 9"]),
            ("ids 101, 102, 103 here", ["101, 102, 103"]),
            ("ids 101/102 here", ["101/102"]),
            ("the widget_count field", ["widget_count"]),
            ("see acme.widgets.core now", ["acme.widgets.core"]),
            ("commit 3f9a1c2b7d now", ["3f9a1c2b7d"]),
            ("edit ~/notes today", ["~/notes"]),
            ("edit settings.toml today", ["settings.toml"]),
            ("run `make test` now", ["`make test`"]),
            ("run it with --force now", ["--force"]),
            ("set mode=fast now", ["mode=fast"]),
            ("it was DEFERRED once", ["DEFERRED"]),
        ],
    )
    def test_names_the_offending_token(self, text: str, tokens: list[str]) -> None:
        assert internal_shorthand(text) == tokens

    @pytest.mark.parametrize(
        "text",
        [
            "and/or, 24/7, CI/CD and e.g. i.e. pass",
            "on 10/10/2026 at 08:00",
            "about 1,000,000 widgets, 12 of them",
            "version 3.14 ships",
            "a deadbeef cafe word",
            "see https://git.acme.example/widgets/issues/42 now",
            "read <https://git.acme.example/widgets/issues/42|the widget issue> now",
        ],
    )
    def test_ordinary_text_and_urls_pass(self, text: str) -> None:
        assert internal_shorthand(text) == []

    def test_a_link_label_is_linted_while_its_url_is_not(self) -> None:
        assert internal_shorthand("read <https://git.acme.example/widgets/issues/42|issue #42> now") == ["issue #42"]

    def test_tokens_come_back_in_reading_order_without_overlap(self) -> None:
        assert internal_shorthand("ticket #104 then widget_count then #9") == ["ticket #104", "widget_count", "#9"]


class TestEvidenceRoundTrip:
    def test_the_stored_fields_come_back_from_the_row(self) -> None:
        card = dataclasses.replace(SAMPLE_CARD, quoted="Thanks, see you soon.")

        restored = QuestionCard.from_row(card.to_evidence(), card.options_json())

        assert restored == card

    def test_a_row_without_a_why_is_a_legacy_row(self) -> None:
        legacy = {"decision": "product_scope", "checked": ["a fact"]}

        assert QuestionCard.from_row(legacy, "") is None
        assert QuestionCard.from_row({}, "") is None

    def test_the_options_keep_the_ask_user_question_shape(self) -> None:
        [first, second] = json.loads(SAMPLE_CARD.options_json())

        assert first == {"label": "Done", "description": "I check which setup is installed.", "recommended": True}
        assert second == {"label": "Later", "description": "I ask you again later."}

    def test_a_card_without_options_stores_no_options(self) -> None:
        assert dataclasses.replace(SAMPLE_CARD, options=()).options_json() == ""


class TestWithQuote:
    def test_a_quote_that_keeps_the_card_valid_is_added_on_one_line(self) -> None:
        card = SAMPLE_CARD.with_quote(SAMPLE_QUESTION, "Thanks,\n  see you   soon.")

        assert card.quoted == "Thanks, see you soon."

    def test_a_quote_that_breaks_the_card_is_left_off(self) -> None:
        card = SAMPLE_CARD.with_quote(SAMPLE_QUESTION, "Please read widget_count first.")

        assert card is SAMPLE_CARD

    def test_the_problems_of_a_quote_are_those_of_the_card_carrying_it(self) -> None:
        assert SAMPLE_CARD.quote_problems(SAMPLE_QUESTION, "Thanks, see you soon.") == []
        [problem] = SAMPLE_CARD.quote_problems(SAMPLE_QUESTION, "See #42 today.")
        assert problem.startswith("the quoted text carries internal shorthand (#42)")

    def test_a_blank_quote_changes_nothing(self) -> None:
        assert SAMPLE_CARD.with_quote(SAMPLE_QUESTION, "  \n ") is SAMPLE_CARD

    def test_a_quote_that_pushes_the_card_over_the_word_cap_is_left_off(self) -> None:
        assert SAMPLE_CARD.with_quote(SAMPLE_QUESTION, "word " * 100) is SAMPLE_CARD


class TestFromEnvelope:
    def test_builds_the_card_from_the_stop_keys(self) -> None:
        card = QuestionCard.from_envelope(
            {
                "user_input_kind": "product_scope",
                "user_input_checked": ["The bot answers direct messages."],
                "user_input_card": {
                    "why": SAMPLE_WHY,
                    "blocker": "Only the owner can say which setup is wanted.",
                    "options": [
                        {"label": "Done", "description": "I check which setup is installed.", "recommended": True},
                        {"label": "Later", "description": "I ask you again later."},
                    ],
                },
            }
        )

        assert card == SAMPLE_CARD

    @pytest.mark.parametrize("result", [{}, {"user_input_kind": "whim", "user_input_card": {"why": "w"}}])
    def test_a_stop_naming_no_owner_decision_has_no_card(self, result: dict[str, object]) -> None:
        assert QuestionCard.from_envelope(result) is None

    @pytest.mark.parametrize(
        "result",
        [
            {"user_input_kind": "product_scope", "user_input_checked": ["x"]},
            {"user_input_kind": "product_scope", "user_input_checked": ["x"], "user_input_card": "text"},
            {"user_input_kind": "product_scope", "user_input_checked": "x", "user_input_card": {"why": "w"}},
            {"user_input_kind": "product_scope", "user_input_checked": ["x"], "user_input_card": {"options": "no"}},
        ],
    )
    def test_a_mistyped_key_leaves_a_gap_the_card_names(self, result: dict[str, object]) -> None:
        card = QuestionCard.from_envelope(result)

        assert card is not None
        assert any("required" in problem or "checked facts" in problem for problem in card.problems("Is it fine?"))
