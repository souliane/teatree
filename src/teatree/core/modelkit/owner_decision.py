"""The closed set of decisions that earn an owner DM; everything else is factory work (#5096)."""

import enum
from collections.abc import Iterable


class OwnerDecision(enum.StrEnum):
    """A decision only the owner can make. A question naming none is recorded internal."""

    CREDENTIALS = "credentials"
    MONEY_OR_PLAN = "money_or_plan"
    PUBLIC_POST = "public_post"
    IRREVERSIBLE = "irreversible"
    PRODUCT_SCOPE = "product_scope"
    ARCHITECTURE = "architecture"


type OwnerEvidence = dict[str, str | list[str]]

OWNER_QUESTION_ROUTE = (
    "Ask the owner only via `t3 <overlay> questions record '<question>' --decision <kind> --checked '<fact>'`, "
    f"<kind> being one of {', '.join(OwnerDecision)} (an access or permission grant is credentials), "
    "listing every fact you checked first; decide everything else yourself."
)

OWNER_ANSWER_ROUTE = (
    "an owner question is answered only by the owner, in its Slack thread; if it no longer applies, "
    "`t3 <overlay> questions dismiss <id> --reason '<evidence>'`."
)


def owner_decision(value: object) -> OwnerDecision | None:
    """*value* as an :class:`OwnerDecision`, or ``None`` when it names none of the six."""
    try:
        return OwnerDecision(str(value))
    except ValueError:
        return None


def owner_evidence(decision: object, checked: Iterable[str]) -> OwnerEvidence | None:
    """The stored proof an owner row needs, or ``None`` for an unknown kind or no non-blank checked fact."""
    kind = owner_decision(decision)
    facts = [fact.strip() for fact in checked if fact.strip()]
    if kind is None or not facts:
        return None
    return {"decision": kind.value, "checked": facts}


__all__ = [
    "OWNER_ANSWER_ROUTE",
    "OWNER_QUESTION_ROUTE",
    "OwnerDecision",
    "OwnerEvidence",
    "owner_decision",
    "owner_evidence",
]
