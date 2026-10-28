"""Owner stops that are not owner decisions: three explicit rows, every other stop stays a card (#5687).

A row fires only when the stop's question is exactly the row's benign closed form (an anchored allow pattern),
its decision kind and required facts match, and no word of the row's lexicon appears anywhere the owner reads
the card. Any other wording records the card; the lexicon is defence in depth behind the allow pattern.
Money and public words to a colleague never match.
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


def _closed_ask(form: str) -> re.Pattern[str]:
    return re.compile(rf"(?:may|can|could|should) (?:i|we) (?:please )?{form}\s*\??", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class OwnerStopMisfit:
    decision: OwnerDecision
    ask: re.Pattern[str]
    action: MisfitAction
    exclusions: re.Pattern[str]
    facts: tuple[re.Pattern[str], ...] = ()

    def fits(self, decision: OwnerDecision, question: str, shown: str) -> bool:
        return (
            decision is self.decision
            and self.ask.fullmatch(" ".join(question.split())) is not None
            and all(fact.search(shown) for fact in self.facts)
            and not self.exclusions.search(shown)
        )


_AGAIN = r"(?: (?:again|once more))?"
_OWNER_KEPT = (
    r"credentials?|permission\w*|privileg\w+|admin\w*|elevat\w+|bypass\w*|overrid\w+|allow-?list\w*|access\w*|sudo|root"
    r"|secrets?|tokens?|passwords?|keys?|logins?|accounts?|grant\w*|widen\w*|expand\w*|scope\w*"
    r"|money|pay\w*|paid|spen[dt]\w*|buy\w*|bill\w*|cost\w*|pric\w+|budget\w*|purchas\w+|subscri\w+"
    r"|comments?|messages?|repl(?:y|ies)|colleagues?|reviewers?|public\w*|posts?"
)
_PUSH_TARGETS = (
    r"protected|main|master|force\w*|upstream|origin|repository|repositories|repos?|forks?"
    r"|release[sd]?|releasing|tags?|remote\w*"
)
_DESTRUCTION = (
    r"destr\w+|wip\w+|los\w+|unsaved|unpushed|uncommitted|no remote|only cop(?:y|ies)|drop\w*|purg\w+"
    r"|overwrit\w+|truncate[sd]?|truncating|databases?|volumes?|data|files?|works?"
    r"|delete[sd]?|deleting|deletion|discard\w*|eras\w+|remov\w+"
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
        _words(_OWNER_KEPT),
        (_words("denied|rejected|blocked"), _words(r"commands?|sub-?agents?|(?:agent|review) starts?")),
    ),
    OwnerStopMisfit(
        OwnerDecision.PUBLIC_POST,
        _closed_ask(
            rf"push (?:the |my |our )?(?:finished|own) (?:pull[- ]request )?branch{_AGAIN}"
            r"(?: after the (?:push )?round limit)?"
        ),
        MisfitAction.REFUSE_AND_DECIDE,
        _words(rf"{_OWNER_KEPT}|{_PUSH_TARGETS}"),
    ),
    OwnerStopMisfit(
        OwnerDecision.IRREVERSIBLE,
        _closed_ask(
            rf"(?:stop|kill|terminate|restart) {_OWN_PROCESS} (?:(?:that|which) )?(?:i|this task) "
            r"(?:started|launched|spawned)(?: for this (?:check|run|task|test))?"
        ),
        MisfitAction.REFUSE_AND_DECIDE,
        _words(rf"{_OWNER_KEPT}|{_DESTRUCTION}"),
    ),
)


def owner_stop_misfit(result: Mapping[str, object]) -> MisfitAction | None:
    """The action for an owner stop that a row proves is no owner decision, or ``None``: record the card."""
    card = QuestionCard.from_envelope(result)
    if card is None or result.get("needs_user_input") is not True:
        return None
    question = str(result.get("user_input_reason", ""))
    options = (f"{option.label} {option.description}" for option in card.options)
    shown = " ".join((question, *card.checked, card.blocker, card.why, *options))
    return next((row.action for row in OWNER_STOP_MISFITS if row.fits(card.decision, question, shown)), None)
