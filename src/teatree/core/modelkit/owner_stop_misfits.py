"""Owner stops that are not owner decisions: three explicit rows, every other stop stays a card (#5687).

A row fires only when its decision kind, every marker and no exclusion matches the stop's whole text,
so a word the rows do not know leaves the card. Money and public words to a colleague never match.
"""

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import QuestionCard


class MisfitAction(enum.StrEnum):
    ENVIRONMENT_FAILURE = "environment_failure"
    REFUSE_AND_DECIDE = "refuse_and_decide"


def _words(alternatives: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class OwnerStopMisfit:
    decision: OwnerDecision
    markers: tuple[re.Pattern[str], ...]
    exclusions: re.Pattern[str]
    action: MisfitAction

    def fits(self, decision: OwnerDecision, text: str) -> bool:
        return (
            decision is self.decision
            and all(marker.search(text) for marker in self.markers)
            and not self.exclusions.search(text)
        )


OWNER_STOP_MISFITS = (
    OwnerStopMisfit(
        OwnerDecision.CREDENTIALS,
        (_words("denied|rejected|blocked"), _words(r"commands?|sub-?agents?|(?:agent|review) starts?")),
        _words("secrets?|tokens?|passwords?|keys?|logins?|accounts?|grant|widen|expand|scope"),
        MisfitAction.ENVIRONMENT_FAILURE,
    ),
    OwnerStopMisfit(
        OwnerDecision.PUBLIC_POST,
        (_words("push(?:ing)?"), _words(r"(?:finished|own|current) (?:pull[- ]request )?branch|own pull[- ]request")),
        _words("comments?|messages?|repl(?:y|ies)|colleagues?"),
        MisfitAction.REFUSE_AND_DECIDE,
    ),
    OwnerStopMisfit(
        OwnerDecision.IRREVERSIBLE,
        (
            _words(r"stop(?:ping)?|kill(?:ing)?|terminat(?:e|ing)"),
            _words(r"process(?:es)?|server|job"),
            _words(
                r"own|(?:i|this task) (?:started|launched|spawned)|(?:started|launched|spawned) by (?:me|this task)"
            ),
        ),
        _words(r"delete[sd]?|deleting|deletion|discard\w*|unpushed|uncommitted|los[et]|wipe|erase|remov\w*"),
        MisfitAction.REFUSE_AND_DECIDE,
    ),
)


def owner_stop_misfit(result: Mapping[str, object]) -> MisfitAction | None:
    """The action for an owner stop that a row proves is no owner decision, or ``None``: record the card."""
    card = QuestionCard.from_envelope(result)
    if card is None or result.get("needs_user_input") is not True:
        return None
    text = " ".join((str(result.get("user_input_reason", "")), *card.checked, card.blocker, card.why))
    return next((row.action for row in OWNER_STOP_MISFITS if row.fits(card.decision, text)), None)
