"""The closed set of decisions that earn an owner DM; everything else is factory work (#5096)."""

import enum


class OwnerDecision(enum.StrEnum):
    """A decision only the owner can make. A question naming none is recorded internal."""

    CREDENTIALS = "credentials"
    MONEY_OR_PLAN = "money_or_plan"
    PUBLIC_POST = "public_post"
    IRREVERSIBLE = "irreversible"
    PRODUCT_SCOPE = "product_scope"
    ARCHITECTURE = "architecture"


OWNER_QUESTION_ROUTE = (
    "Ask the owner only via `t3 <overlay> questions record '<one question ending in ?>' --decision <kind> "
    "--checked '<fact>' --why '<one sentence on why you ask>' --blocker '<what stops you>' "
    '--options \'[{"label": "<answer>", "description": "<what happens next>", "recommended": true}, ...]\'`, '
    f"<kind> being one of {', '.join(OwnerDecision)} (an access or permission grant is credentials), "
    "listing every fact you checked first and 2 to 4 options with the recommended one first, or none; "
    "at most 120 words in all; plain English everywhere, quotes included (no ids, snake_case names, paths or "
    "commands); decide everything else yourself."
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


__all__ = [
    "OWNER_ANSWER_ROUTE",
    "OWNER_QUESTION_ROUTE",
    "OwnerDecision",
    "owner_decision",
]
