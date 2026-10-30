"""Shared owner-channel test helpers: ask the owner a genuine decision, answer it through the real Slack seam."""

import copy
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
    decision=OwnerDecision.ARCHITECTURE,
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


def stop_with_wording(kind: str, field: str, wording: str) -> dict[str, object]:
    """The benign stop of *kind* with *wording* added in exactly one field the owner reads or an operator stores."""
    question, facts = BENIGN_STOPS[kind]
    stop = owner_stop(question, kind, *facts)
    card = copy.deepcopy(stop["user_input_card"])
    assert isinstance(card, dict)
    if field == "fact":
        stop["user_input_checked"] = [*(facts or OWNER_CARD.checked), wording]
    elif field in {"why", "blocker"}:
        card[field] = wording
    else:
        card["options"][1][field] = wording
    return stop | {"user_input_card": card}


LOOKALIKE_DENIAL = "The sub-agent start was denied by the current route."

#: Stops that borrow a benign row's words but ask a decision only the owner makes, or word it in a way no row knows.
OWNER_KEPT_LOOKALIKES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("credentials", "May I use an admin credential for it?", (LOOKALIKE_DENIAL,)),
    ("credentials", "May I bypass the permission gate so the command runs?", (LOOKALIKE_DENIAL,)),
    ("credentials", "May I elevate the privileges so the command runs?", (LOOKALIKE_DENIAL,)),
    ("credentials", "May I use a personal credential for it?", (LOOKALIKE_DENIAL,)),
    ("credentials", "May I add the command to the allowlist?", (LOOKALIKE_DENIAL,)),
    ("credentials", "May I borrow the deploy identity so the command runs?", (LOOKALIKE_DENIAL,)),
    ("irreversible", "May I terminate the job I started, even though it would destroy the only copy?", ()),
    ("irreversible", "May I stop the server I started, since stopping wipes the database volume?", ()),
    ("irreversible", "May I kill the process I started, which holds unsaved work that exists on no remote?", ()),
    ("irreversible", "May I stop the job I started, given that it loses the only copy?", ()),
    ("irreversible", "May I stop the server I started and clear its history?", ()),
    ("public_post", "May I force-push the finished branch over the protected main branch?", ()),
    ("public_post", "May I push the finished branch to the public upstream repository?", ()),
    ("public_post", "May I push the finished branch to the mirror?", ()),
    ("money_or_plan", "May I start the sub-agent again?", (LOOKALIKE_DENIAL,)),
    ("public_post", "May I start the sub-agent again?", (LOOKALIKE_DENIAL,)),
    ("irreversible", "May I start the sub-agent again?", (LOOKALIKE_DENIAL,)),
    ("credentials", "May I push the finished branch once more?", ()),
    ("money_or_plan", "May I stop the dev server I started for this check?", ()),
)

#: The question and facts of a stop that a misfit row fires on when the rest of the card is the default one.
BENIGN_STOPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "credentials": ("May I start the sub-agent again?", (LOOKALIKE_DENIAL,)),
    "public_post": ("May I push the finished branch once more?", ()),
    "irreversible": ("May I stop the dev server I started for this check?", ()),
}

#: (kind, field, wording): ONE unlisted wording in ONE field of an otherwise benign stop must reach the owner as a card.
UNLISTED_WORDING: tuple[tuple[str, str, str], ...] = (
    ("credentials", "fact", "Only the owner can authorize it."),
    ("credentials", "fact", "It needs write rights on the shared repository."),
    ("credentials", "fact", "It needs the SSO session approved by you."),
    ("credentials", "why", "Enable the bot for the organisation first."),
    ("credentials", "blocker", "Only the owner can authorize it."),
    ("credentials", "label", "Whitelist the host"),
    ("credentials", "label", "Permit it"),
    ("credentials", "description", "I permit it."),
    ("irreversible", "fact", "Its in-memory results exist only in that process."),
    ("irreversible", "fact", "Stopping ends the only run that produced them."),
    ("irreversible", "why", "Its in-memory results exist only in that process."),
    ("irreversible", "blocker", "Stopping ends the only run that produced them."),
    ("irreversible", "label", "Stop it anyway"),
    ("irreversible", "description", "Its in-memory results are gone."),
    ("public_post", "fact", "It publishes the commits to everyone."),
    ("public_post", "why", "It publishes the commits to everyone."),
    ("public_post", "blocker", "It publishes the commits to everyone."),
    ("public_post", "label", "Publish them"),
    ("public_post", "description", "It publishes the commits to everyone."),
)


class OwnerDecisionFields(TypedDict):
    card: QuestionCard


OWNER_DECISION = OwnerDecisionFields(card=OWNER_CARD)


def owner_card(decision: OwnerDecision = OwnerDecision.ARCHITECTURE, *checked: str) -> QuestionCard:
    return dataclasses.replace(OWNER_CARD, decision=decision, checked=checked or OWNER_CARD.checked)


def legacy_owner_row(
    question: str, *, decision: OwnerDecision = OwnerDecision.ARCHITECTURE, **fields: object
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
