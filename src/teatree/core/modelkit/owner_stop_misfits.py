"""Owner stops that are not owner decisions: three explicit rows, every other stop stays a card (#5687).

A row fires only when every text of the stop is one of its closed forms: the question is the row's benign ask,
and each checked fact, the why, the blocker and each option label and description is a closed filler sentence
(the credentials row also accepts the denied start its question is about). Text outside the closed forms, in
any of them, means no row fits and the stop records the card. Money and public words to a colleague never match.
"""

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import QuestionCard


class MisfitAction(enum.StrEnum):
    ENVIRONMENT_FAILURE = "environment_failure"
    REFUSE_AND_DECIDE = "refuse_and_decide"
    BACKGROUND_RETRY = "background_retry"


def _words(alternatives: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


def _closed(forms: str) -> re.Pattern[str]:
    return re.compile(rf"(?:{forms})[.?!]?", re.IGNORECASE)


def _closed_ask(form: str) -> re.Pattern[str]:
    return _closed(rf"(?:may|can|could|should) (?:i|we) (?:please )?{form}")


def _squashed(text: str) -> str:
    return " ".join(text.split())


@dataclass(frozen=True, slots=True)
class OwnerStopMisfit:
    decision: OwnerDecision
    ask: re.Pattern[str]
    action: MisfitAction
    support: re.Pattern[str]
    facts: tuple[re.Pattern[str], ...] = ()

    def fits(self, decision: OwnerDecision, question: str, texts: Sequence[str]) -> bool:
        stop = " ".join((question, *texts))
        return (
            decision is self.decision
            and self.ask.fullmatch(_squashed(question)) is not None
            and all(self.support.fullmatch(_squashed(text)) for text in texts)
            and all(fact.search(stop) for fact in self.facts)
        )


_AGAIN = r"(?: (?:again|once more))?"
_FILLER = (
    r"no standing answer covers it|only the owner can choose|the answer changes what the factory does next"
    r"|yes|no|i go ahead|i leave it as it is"
)
_DENIED_START = (
    r"the (?:(?:shell|review|agent) )?(?:command|sub-?agent start|start) was (?:denied|rejected|blocked)"
    r"(?: by the (?:current route|policy|venue))?"
)
_RETRY_TARGET = (
    r"(?:it|that|this|them|the (?:(?:denied|rejected|blocked) )?(?:command|sub-?agent|(?:agent|review) start))"
)
_OWN_PROCESS = r"(?:the |this |my |our )?(?:dev |local |test )?(?:process|job|server|worker|container)"

OWNER_STOP_MISFITS = (
    OwnerStopMisfit(
        OwnerDecision.CREDENTIALS,
        _closed_ask(rf"(?:retry|rerun|re-run|run|start|launch|try)(?: {_RETRY_TARGET})?{_AGAIN}"),
        MisfitAction.ENVIRONMENT_FAILURE,
        _closed(rf"{_FILLER}|{_DENIED_START}"),
        (_words("denied|rejected|blocked"), _words(r"commands?|sub-?agents?|(?:agent|review) starts?")),
    ),
    OwnerStopMisfit(
        OwnerDecision.PUBLIC_POST,
        _closed_ask(
            rf"push (?:the |my |our )?(?:finished|own) (?:pull[- ]request )?branch{_AGAIN}"
            r"(?: after the (?:push )?round limit)?"
        ),
        MisfitAction.REFUSE_AND_DECIDE,
        _closed(_FILLER),
    ),
    OwnerStopMisfit(
        OwnerDecision.IRREVERSIBLE,
        _closed_ask(
            rf"(?:stop|kill|terminate|restart) {_OWN_PROCESS} (?:(?:that|which) )?(?:i|this task) "
            r"(?:started|launched|spawned)(?: for this (?:check|run|task|test))?"
        ),
        MisfitAction.REFUSE_AND_DECIDE,
        _closed(_FILLER),
    ),
)


_AUTOMATIC_REVIEW_ASK = re.compile(
    r"(?:Can I have one-time approval to rerun the focused regression tests|"
    r"Can you approve local command access so testing can continue|"
    r"(?:Can|May|Could|Should) (?:I|we) (?:please )?(?:re-?run|retry|run) "
    r"(?:the |my )?(?:focused )?(?:tests?|command)(?: in the background)?)\?",
    re.IGNORECASE,
)
_AUTOMATIC_REVIEW_FACT = re.compile(
    r"The automatic approval review (?:refused|rejected|denied|blocked) the "
    r"(?:required skill preamble command|focused regression tests|test command)"
    r"(?: as foreground heavy work)?[.!]?",
    re.IGNORECASE,
)
_AUTOMATIC_REVIEW_FILLER = _closed(_FILLER)


def _automatic_review_refusal(card: QuestionCard, question: str) -> bool:
    options = (text for option in card.options for text in (option.label, option.description))
    readable = (card.why, card.blocker, *options)
    return (
        card.decision is OwnerDecision.CREDENTIALS
        and _AUTOMATIC_REVIEW_ASK.fullmatch(_squashed(question)) is not None
        and bool(card.checked)
        and all(_AUTOMATIC_REVIEW_FACT.fullmatch(_squashed(fact)) for fact in card.checked)
        and all(_AUTOMATIC_REVIEW_FILLER.fullmatch(_squashed(text)) for text in readable)
    )


def owner_stop_misfit(result: Mapping[str, object]) -> MisfitAction | None:
    """The action for an owner stop that a row proves is no owner decision, or ``None``: record the card."""
    card = QuestionCard.from_envelope(result)
    if card is None or result.get("needs_user_input") is not True:
        return None
    question = str(result.get("user_input_reason", ""))
    if _automatic_review_refusal(card, question):
        return MisfitAction.BACKGROUND_RETRY
    options = (text for option in card.options for text in (option.label, option.description))
    texts = tuple(filter(None, (*card.checked, card.blocker, card.why, *options)))
    return next((row.action for row in OWNER_STOP_MISFITS if row.fits(card.decision, question, texts)), None)
