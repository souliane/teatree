"""The owner-question message: the approved layout as plain text and as Slack blocks (#4990)."""

import datetime as dt
import re
from typing import Any, cast

from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, word_count
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.owner_question_message import (
    answered_line,
    bump_text,
    closed_message,
    digest_text,
    render_blocks,
    render_text,
    shown_problems_for,
    withheld_message,
)
from tests._owner_channel import legacy_owner_row

_SAMPLE_QUESTION = "Can you mention the bot once (type @ and its name) in any Slack channel?"
_SAMPLE_WHY = "This tells me whether it is set up for channels or only for our direct messages."
_SAMPLE_CARD = QuestionCard(
    decision=OwnerDecision.PRODUCT_SCOPE,
    checked=("The bot answers direct messages.",),
    blocker="Only the owner can say which setup is wanted.",
    why=_SAMPLE_WHY,
    options=(
        CardOption("Done", "I check which setup is installed.", recommended=True),
        CardOption("Later", "I ask you again later."),
    ),
)
_SAMPLE_TEXT = """\
Can you mention the bot once (type @ and its name) in any Slack channel?
This tells me whether it is set up for channels or only for our direct messages.

[Done] (recommended) - I check which setup is installed.
[Later] - I ask you again later.

Or reply in this thread.
ref: question {ref}"""

_NO_OPTIONS_TEXT = """\
Can I archive the old widget logs?
They take space and nobody reads them.

Reply in this thread.
ref: question {ref}"""

_DRAFT_TEXT = """\
May I post this reply for you?
A drafted answer is waiting, and nothing is posted until you decide.

> Thanks, alice. The fix ships on Monday.

[Post it] (recommended) - I post the reply as written.
[Do not post] - I drop the reply.

Or reply in this thread.
ref: question {ref}"""

_LEGACY_TEXT = """\
Should the acme widget go to production?

[Yes] - I record this as your answer.
[No] - I record this as your answer.

Or reply in this thread.
ref: question {ref}"""

_LARGEST_QUESTION = "Which of these four rollout plans should the acme widget team follow?"
_LARGEST_QUOTE = (
    "We could ship the widget to alice and bob first, then wait one full week before opening it to everyone "
    "else, which keeps the support load small and gives us time to fix whatever they find, and we would also "
    "write a short note for the support team so that nobody is surprised when questions arrive."
)
_LARGEST_TEXT = """\
Which of these four rollout plans should the acme widget team follow?
Each plan trades speed against the risk of a bad release.

> We could ship the widget to alice and bob first, then wait one full week before opening it to everyone else, \
which keeps the support load small and gives us time to fix whatever they find, and we would also write a short \
note for the support team so that nobody is surprised when questions arrive.

[Staged] (recommended) - I ship to alice and bob first and wait a week.
[Everyone] - I ship to everybody today.
[Pilot] - I ship to bob only and watch it.
[Hold] - I ship nothing until you say so.

Or reply in this thread.
ref: question {ref}"""


def _card_row(question: str, card: QuestionCard) -> DeferredQuestion:
    return DeferredQuestion.record(question, card=card)


def _no_options_row() -> DeferredQuestion:
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=("The logs are older than a year.",),
        blocker="Archiving cannot be undone.",
        why="They take space and nobody reads them.",
    )
    return _card_row("Can I archive the old widget logs?", card)


def _draft_row() -> DeferredQuestion:
    card = QuestionCard(
        decision=OwnerDecision.PUBLIC_POST,
        checked=("A reply was drafted.",),
        blocker="Only the owner approves a post made in their name.",
        why="A drafted answer is waiting, and nothing is posted until you decide.",
        options=(
            CardOption("Post it", "I post the reply as written.", recommended=True),
            CardOption("Do not post", "I drop the reply."),
        ),
        quoted="Thanks, alice. The fix ships on Monday.",
    )
    return _card_row("May I post this reply for you?", card)


def _largest_row() -> DeferredQuestion:
    card = QuestionCard(
        decision=OwnerDecision.PRODUCT_SCOPE,
        checked=("Four plans were drawn up.",),
        blocker="Only the owner picks the rollout.",
        why="Each plan trades speed against the risk of a bad release.",
        options=(
            CardOption("Staged", "I ship to alice and bob first and wait a week.", recommended=True),
            CardOption("Everyone", "I ship to everybody today."),
            CardOption("Pilot", "I ship to bob only and watch it."),
            CardOption("Hold", "I ship nothing until you say so."),
        ),
        quoted=_LARGEST_QUOTE,
    )
    return _card_row(_LARGEST_QUESTION, card)


def _legacy_row() -> DeferredQuestion:
    return legacy_owner_row(
        "Should the acme widget go to production?",
        options_json='[{"label": "Yes"}, {"label": "No"}]',
    )


def _answered(row: DeferredQuestion, answer: str) -> DeferredQuestion:
    answered = row.apply_answer(answer, resolved_via="slack")
    assert answered is not None
    return answered


def _all_shapes() -> dict[str, tuple[DeferredQuestion, str]]:
    return {
        "sample": (_card_row(_SAMPLE_QUESTION, _SAMPLE_CARD), _SAMPLE_TEXT),
        "no options": (_no_options_row(), _NO_OPTIONS_TEXT),
        "largest": (_largest_row(), _LARGEST_TEXT),
        "draft": (_draft_row(), _DRAFT_TEXT),
        "legacy": (_legacy_row(), _LEGACY_TEXT),
    }


def _blocks(row: DeferredQuestion) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", render_blocks(row))


def _lines_of(blocks: list[dict[str, Any]]) -> list[str]:
    """The text lines the blocks show, in order, as the plain text spells them."""
    lines: list[str] = []
    pending_button = ""
    for block in blocks:
        if block["type"] == "section":
            head, _, rest = block["text"]["text"].partition("\n")
            lines += [head.removeprefix("*").removesuffix("*") if not lines else head, *([rest] if rest else [])]
        elif block["type"] == "actions":
            [button] = block["elements"]
            pending_button = f"[{button['text']['text']}] "
        else:
            [element] = block["elements"]
            lines.append(pending_button + element["text"] if pending_button else element["text"])
            pending_button = ""
    return lines


class TestTheApprovedSample(TestCase):
    def test_the_approved_sample_renders_byte_for_byte(self) -> None:
        row = _card_row(_SAMPLE_QUESTION, _SAMPLE_CARD)

        text = render_text(row)

        assert text == _SAMPLE_TEXT.format(ref=row.pk)
        assert word_count(text) == 49
        blocks_text = str(render_blocks(row))
        for stored in ("The bot answers direct messages.", "Only the owner can say which setup is wanted."):
            assert stored not in text
            assert stored not in blocks_text
        assert row.evidence["blocker"] == "Only the owner can say which setup is wanted."
        assert row.evidence["checked"] == ["The bot answers direct messages."]


class TestTheGoldens(TestCase):
    def test_golden_text(self) -> None:
        for name, (row, golden) in _all_shapes().items():
            with self.subTest(shape=name):
                assert render_text(row) == golden.format(ref=row.pk)

    def test_golden_blocks(self) -> None:
        row = _card_row(_SAMPLE_QUESTION, _SAMPLE_CARD)

        assert render_blocks(row) == [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_SAMPLE_QUESTION}*\n{_SAMPLE_WHY}"}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Done"},
                        "action_id": "owner-question-option-1",
                        "value": "1",
                        "style": "primary",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "(recommended) - I check which setup is installed."}],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Later"},
                        "action_id": "owner-question-option-2",
                        "value": "2",
                    }
                ],
            },
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "- I ask you again later."}]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Or reply in this thread."}]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"ref: question {row.pk}"}]},
        ]

    def test_a_card_without_options_has_no_buttons(self) -> None:
        row = _no_options_row()

        kinds = [block["type"] for block in render_blocks(row)]

        assert kinds == ["section", "context", "context"]

    def test_a_quote_gets_its_own_section_before_the_options(self) -> None:
        blocks = render_blocks(_draft_row())

        assert blocks[1] == {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "> Thanks, alice. The fix ships on Monday."},
        }


class TestLayoutProperties(TestCase):
    def test_layout_properties(self) -> None:
        for name, (row, _golden) in _all_shapes().items():
            with self.subTest(shape=name):
                text, blocks = render_text(row), _blocks(row)
                lines = text.split("\n")
                assert not text.startswith("\n")
                assert not text.endswith("\n")
                assert "\n\n\n" not in text
                assert word_count(text) <= 120
                for banned in ("deferred", "Pending question", "typed reply", "What I checked", "What blocks me"):
                    assert banned.lower() not in text.lower(), banned
                assert lines[-2] in {"Or reply in this thread.", "Reply in this thread."}
                assert lines[-1] == f"ref: question {row.pk}"
                options = [line for line in lines if re.match(r"^\[.+\]( \(recommended\))? - ", line)]
                buttons = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
                assert len(options) == len(buttons)
                primaries = [i for i, button in enumerate(buttons) if button.get("style") == "primary"]
                assert primaries == ([0] if "(recommended)" in text else [])
                if options and "(recommended)" in text:
                    assert "(recommended)" in options[0]
                assert _lines_of(blocks) == [line for line in lines if line]
                added = set(re.findall(r"\d+", text)) - set(
                    re.findall(r"\d+", row.question + row.options_json + str(row.evidence))
                )
                assert added <= {str(row.pk)}

    def test_the_largest_card_is_exactly_120_words_and_inside_slack_limits(self) -> None:
        row = _largest_row()

        blocks = _blocks(row)

        assert word_count(render_text(row)) == 120
        assert len(blocks) <= 50
        for block in blocks:
            if block["type"] == "section":
                assert 0 < len(block["text"]["text"]) <= 3000
            elif block["type"] == "actions":
                assert all(0 < len(b["text"]["text"]) <= 75 for b in block["elements"])
            else:
                assert all(0 < len(e["text"]) <= 3000 for e in block["elements"])


class TestClosingTheCard(TestCase):
    def test_a_tapped_option_becomes_you_chose_with_its_consequence(self) -> None:
        row = _answered(_card_row(_SAMPLE_QUESTION, _SAMPLE_CARD), "Done")

        message = closed_message(row, answered_line(row))

        assert message.text == (
            f"{_SAMPLE_QUESTION}\n{_SAMPLE_WHY}\n\n"
            f"You chose: Done - I check which setup is installed.\nref: question {row.pk}"
        )
        assert all(block["type"] != "actions" for block in message.blocks)
        assert message.blocks[-1] == {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"ref: question {row.pk}"}],
        }
        assert {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "You chose: Done - I check which setup is installed."},
        } in message.blocks

    def test_a_typed_answer_that_is_no_option_reads_you_answered(self) -> None:
        row = _answered(_card_row(_SAMPLE_QUESTION, _SAMPLE_CARD), "tomorrow, please")

        assert answered_line(row) == "You answered: tomorrow, please"

    def test_the_quote_stays_on_a_closed_card(self) -> None:
        row = _answered(_draft_row(), "Post it")

        message = closed_message(row, answered_line(row))

        assert "> Thanks, alice. The fix ships on Monday." in message.text
        assert "You chose: Post it - I post the reply as written." in message.text

    def test_the_withheld_root_says_the_question_will_come_back_in_plain_words(self) -> None:
        message = withheld_message()

        assert message.text == "I will ask this again in plain words."
        assert message.blocks == [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "I will ask this again in plain words."}]}
        ]


class TestTheRePingAndTheDigest(TestCase):
    def _asked(self, hours_ago: float) -> tuple[DeferredQuestion, dt.datetime]:
        now = timezone.now()
        row = _card_row(_SAMPLE_QUESTION, _SAMPLE_CARD)
        DeferredQuestion.objects.filter(pk=row.pk).update(created_at=now - dt.timedelta(hours=hours_ago))
        row.refresh_from_db()
        return row, now

    def test_the_bump_names_the_hours_and_both_ways_to_answer(self) -> None:
        row, now = self._asked(1)

        assert bump_text(row, now=now) == (
            "Still waiting for your decision; I asked 1 hour ago. Tap an option above, or reply here."
        )

    def test_the_bump_counts_days_from_48_hours(self) -> None:
        row, now = self._asked(47)
        assert "I asked 47 hours ago" in bump_text(row, now=now)
        row, now = self._asked(72)
        assert "I asked 3 days ago" in bump_text(row, now=now)

    def test_a_question_without_options_only_asks_for_a_reply(self) -> None:
        row = _no_options_row()
        now = timezone.now() + dt.timedelta(hours=2)

        assert bump_text(row, now=now) == "Still waiting for your decision; I asked 2 hours ago. Reply here."

    def test_the_digest_states_counts_and_age_only(self) -> None:
        old, now = self._asked(72)
        newer = _no_options_row()

        assert digest_text([old, newer], now=now) == (
            "2 decisions are waiting for you; the oldest was asked 3 days ago. Each one is in its own thread."
        )

    def test_a_single_waiting_decision_reads_in_the_singular(self) -> None:
        row, now = self._asked(5)

        assert digest_text([row], now=now) == (
            "1 decision is waiting for you; it was asked 5 hours ago. It is in its own thread."
        )


class TestAnOldRowIsCheckedWhereItIsSent(TestCase):
    def test_a_clean_legacy_row_has_no_problems(self) -> None:
        assert shown_problems_for(_legacy_row()) == []

    def test_a_legacy_row_naming_an_internal_profile_is_refused(self) -> None:
        row = legacy_owner_row("Is acme_only the right scope profile?")

        [problem] = shown_problems_for(row)

        assert "acme_only" in problem

    def test_a_card_row_passes_by_construction(self) -> None:
        assert shown_problems_for(_card_row(_SAMPLE_QUESTION, _SAMPLE_CARD)) == []

    def test_a_legacy_question_over_120_words_is_refused(self) -> None:
        row = legacy_owner_row(" ".join(["word"] * 130) + "?")

        assert "words; at most 120" in shown_problems_for(row)[0]

    def test_the_word_deferred_is_refused_in_any_case(self) -> None:
        row = legacy_owner_row("Was the widget Deferred while you were away?")

        assert "Deferred" in shown_problems_for(row)[0]
