"""``t3 teatree questions record`` records an owner question only as a short, plain card (#4990)."""

import io
import json

from django.core.management import call_command
from django.test import TestCase

from teatree.core.modelkit.owner_decision import OWNER_QUESTION_ROUTE, OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, plain_text, word_count
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.question_text import options_digest
from tests._owner_channel import assert_a_plain_card

_OPTIONS = [
    {"label": "Rotate it", "description": "I rotate the token today.", "recommended": True},
    {"label": "Leave it", "description": "I keep the current token."},
]
_WHY = "The token expires soon and nobody owns its rotation."
_BLOCKER = "Only the owner can approve a new credential."


def _record(question: str, *flags: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        call_command("questions", "record", question, *flags, stdout=out, stderr=err)
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    return code, out.getvalue(), err.getvalue()


def _complete(*, options: list[dict[str, object]] | None = None) -> tuple[str, ...]:
    return (
        "--decision",
        "credentials",
        "--checked",
        "The runbook names no rotation owner.",
        "--why",
        _WHY,
        "--blocker",
        _BLOCKER,
        "--options",
        json.dumps(_OPTIONS if options is None else options),
    )


class TestAJargonQuestionIsRefused(TestCase):
    def test_a_jargon_question_is_refused_and_names_each_token(self) -> None:
        unmarked = [{"label": "Yes", "description": "I go ahead."}, {"label": "No", "description": "I stop."}]
        question = "Is widget_count (101, 102, 103) ready under rule 1 and ticket 104 and #105, or is it deferred?"

        code, out, err = _record(
            question,
            "--decision",
            "irreversible",
            "--checked",
            "The count was read from the log.",
            "--why",
            "The count decides the next step.",
            "--options",
            json.dumps(unmarked),
        )

        assert code == 2
        assert out == ""
        assert not DeferredQuestion.objects.exists()
        for token in ("widget_count", "101, 102, 103", "rule 1", "ticket 104", "#105", "deferred"):
            assert token in err, token
        assert "the blocker is required" in err
        assert "mark exactly one option as recommended" in err
        assert OWNER_QUESTION_ROUTE in err


class TestACompleteCardIsRecorded(TestCase):
    def test_the_card_is_stored_and_reads_plainly(self) -> None:
        code, out, _err = _record("Can I rotate the deploy token?", *_complete())

        assert code == 0
        [row] = list(DeferredQuestion.owner_pending())
        assert f"recorded #{row.pk}." in out
        assert row.evidence["why"] == _WHY
        assert row.evidence["blocker"] == _BLOCKER
        assert row.evidence["checked"] == ["The runbook names no rotation owner."]
        assert json.loads(row.options_json) == _OPTIONS
        assert row.options_hash == options_digest(_OPTIONS)
        text = assert_a_plain_card(row, _BLOCKER, "The runbook")
        assert text.splitlines()[0] == "Can I rotate the deploy token?"

    def test_a_card_without_options_asks_for_a_reply_in_the_thread(self) -> None:
        flags = tuple(flag for flag in _complete() if flag != json.dumps(_OPTIONS))[:-1]

        code, _out, _err = _record("Can I rotate the deploy token?", *flags)

        assert code == 0
        [row] = list(DeferredQuestion.owner_pending())
        assert row.options_json == ""
        assert row.options_hash == ""

    def test_the_default_marker_is_the_decision_and_the_question_fingerprint(self) -> None:
        _record("Can I rotate the deploy token?", *_complete())
        _record("Can I rotate the deploy token?", *_complete())

        assert DeferredQuestion.objects.count() == 1


def _four_options_totalling(question: str, words: int) -> list[dict[str, object]]:
    """Four options whose rows bring the whole card to exactly *words* words."""
    options: list[dict[str, object]] = [
        {"label": f"Option {name}", "description": "I do it.", "recommended": name == "A"} for name in "ABCD"
    ]
    card = QuestionCard(
        decision=OwnerDecision.CREDENTIALS,
        checked=("x",),
        blocker="b",
        why=_WHY,
        options=tuple(
            CardOption(str(o["label"]), str(o["description"]), recommended=bool(o["recommended"])) for o in options
        ),
    )
    padding = words - word_count(plain_text(card.view(question)))
    for index, option in enumerate(options):
        option["description"] = "I do it." + " ok" * (padding // 4 + (index < padding % 4))
    return options


class TestTheCardIsCappedAt120Words(TestCase):
    QUESTION = "Can I rotate the deploy token?"

    def test_a_card_of_exactly_120_words_records(self) -> None:
        code, _out, err = _record(self.QUESTION, *_complete(options=_four_options_totalling(self.QUESTION, 120)))

        assert code == 0, err
        assert DeferredQuestion.owner_pending().count() == 1

    def test_a_card_of_121_words_is_refused_naming_its_count_and_the_limit(self) -> None:
        code, _out, err = _record(self.QUESTION, *_complete(options=_four_options_totalling(self.QUESTION, 121)))

        assert code == 2
        assert "the card is 121 words; at most 120" in err
        assert not DeferredQuestion.objects.exists()


class TestListJsonCarriesTheCard(TestCase):
    def test_marker_audience_and_evidence_are_listed(self) -> None:
        _record("Can I rotate the deploy token?", *_complete())
        out = io.StringIO()

        call_command("questions", "list", "--json", stdout=out)

        [listed] = json.loads(out.getvalue())
        assert listed["audience"] == "owner_question"
        assert listed["dedupe_marker"].startswith("credentials:")
        assert listed["evidence"]["blocker"] == _BLOCKER


class TestAnInvalidOptionsListIsRefused(TestCase):
    def test_options_that_are_not_a_json_list_are_refused(self) -> None:
        flags = list(_complete())
        flags[-1] = "not json"

        code, _out, err = _record("Can I rotate the deploy token?", *flags)

        assert code == 2
        assert "--options must be a JSON list" in err
        assert not DeferredQuestion.objects.exists()
