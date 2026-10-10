"""Shared owner-channel test helpers: ask the owner a genuine decision, answer it through the real Slack seam."""

import dataclasses
import json
from typing import TypedDict
from unittest.mock import MagicMock

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, internal_shorthand, plain_text
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_binding import BoundAnswer, answer_from_click, apply_bound_answer

OWNER_SLACK_ID = "U1"

OWNER_CARD = QuestionCard(
    decision=OwnerDecision.PRODUCT_SCOPE,
    checked=("No standing answer covers it.",),
    blocker="Only the owner can choose.",
    why="The answer changes what the factory does next.",
    options=(
        CardOption("Yes", "I go ahead.", recommended=True),
        CardOption("No", "I leave it as it is."),
    ),
)


def owner_stop(reason: str, kind: str = "credentials", *checked: str) -> dict[str, object]:
    """The envelope keys of a headless stop that asks the owner a complete card."""
    return {
        "needs_user_input": True,
        "user_input_reason": reason,
        "user_input_kind": kind,
        "user_input_checked": list(checked or OWNER_CARD.checked),
        "user_input_card": {
            "why": OWNER_CARD.why,
            "blocker": OWNER_CARD.blocker,
            "options": OWNER_CARD.option_dicts(),
        },
    }


class OwnerDecisionFields(TypedDict):
    card: QuestionCard


OWNER_DECISION = OwnerDecisionFields(card=OWNER_CARD)


def owner_card(decision: OwnerDecision = OwnerDecision.PRODUCT_SCOPE, *checked: str) -> QuestionCard:
    return dataclasses.replace(OWNER_CARD, decision=decision, checked=checked or OWNER_CARD.checked)


def legacy_owner_row(
    question: str, *, decision: OwnerDecision = OwnerDecision.PRODUCT_SCOPE, **fields: object
) -> DeferredQuestion:
    """An owner row as recorded before cards existed: evidence holds only the decision and the checked facts."""
    return DeferredQuestion.objects.create(
        question=question,
        audience=DeferredQuestion.Audience.OWNER_QUESTION,
        evidence={"decision": decision.value, "checked": ["A fact checked before asking."]},
        **fields,
    )


def answer_on_slack(question: DeferredQuestion, text: str) -> None:
    assert apply_bound_answer(BoundAnswer(question=question, answer=text))


def tap_on_slack(question: DeferredQuestion, label: str) -> bool:
    """The owner taps the button *label* on *question*'s posted card; ``True`` when the tap answered it."""
    if not question.slack_ts:
        DeferredQuestion.objects.filter(pk=question.pk).update(
            slack_channel="D0DEMOOWNER", slack_ts=f"1800000000.{question.pk:06d}"
        )
        question.refresh_from_db()
    labels = [option["label"] for option in json.loads(question.options_json)]
    backend = MagicMock()
    backend.user_id = OWNER_SLACK_ID
    payload = {
        "type": "block_actions",
        "user": {"id": OWNER_SLACK_ID},
        "channel": {"id": question.slack_channel},
        "message": {"ts": question.slack_ts},
        "actions": [
            {
                "action_id": f"owner-question-option-{labels.index(label) + 1}",
                "value": str(labels.index(label) + 1),
                "text": {"type": "plain_text", "text": label},
            }
        ],
    }
    return answer_from_click(payload, backend=backend)


def shown_text(row: DeferredQuestion) -> str:
    """What the owner reads for *row*: its card as plain text."""
    card = QuestionCard.from_row(row.evidence, row.options_json)
    assert card is not None, f"row {row.pk} was not recorded as a card"
    return plain_text(card.view(row.question, ref=row.pk))


def assert_a_plain_card(row: DeferredQuestion, *absent: str) -> str:
    """*row* is an owner card that passes every check, reads without shorthand, and shows none of *absent*."""
    card = QuestionCard.from_row(row.evidence, row.options_json)
    assert card is not None, f"row {row.pk} was not recorded as a card"
    assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION
    assert card.problems(row.question) == []
    assert not card.options or card.options[0].recommended
    text = shown_text(row)
    assert internal_shorthand(text.rsplit("\n", 1)[0]) == []
    assert [token for token in absent if token in text] == []
    return text
