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


OWNER_RAISABLE_DECISIONS = (
    OwnerDecision.CREDENTIALS,
    OwnerDecision.MONEY_OR_PLAN,
    OwnerDecision.PUBLIC_POST,
    OwnerDecision.IRREVERSIBLE,
    OwnerDecision.ARCHITECTURE,
)
OWNER_KEPT_DECISIONS = frozenset(
    {
        OwnerDecision.CREDENTIALS,
        OwnerDecision.MONEY_OR_PLAN,
        OwnerDecision.PUBLIC_POST,
        OwnerDecision.IRREVERSIBLE,
    }
)
HELD_PR_MARKER_PREFIX = "owner-wait:held-pr:"


def owner_kept_decision(decision: OwnerDecision | None) -> bool:
    return decision is None or decision in OWNER_KEPT_DECISIONS


OWNER_QUESTION_ROUTE = (
    "Ask the owner only via `t3 <overlay> questions record '<one question ending in ?>' --decision <kind> "
    "--checked '<fact>' --why '<one sentence on why you ask>' --blocker '<what stops you>' "
    '--options \'[{"label": "<answer>", "description": "<what happens next>", "recommended": true}, ...]\'`, '
    f"<kind> being one of {', '.join(OWNER_RAISABLE_DECISIONS)} (an access or permission grant is credentials), "
    "listing every fact you checked first and 2 to 4 options with the recommended one first, or none; "
    "at most 120 words in all; plain English everywhere, quotes included (no ids, snake_case names, paths or "
    "commands); include `--proposed-text '<exact wording>'` for a rules-file change; decide everything else yourself."
)

OWNER_ANSWER_ROUTE = (
    "an owner answer arrives in its Slack thread; a coordinator may dismiss a settled architecture card "
    "with `t3 <overlay> questions dismiss <id> --reason '<evidence>'`; "
    "a dispatched agent cannot dismiss a live owner card."
)


def owner_decision(value: object) -> OwnerDecision | None:
    """*value* as an :class:`OwnerDecision`, or ``None`` when it names none of the six."""
    try:
        return OwnerDecision(str(value))
    except ValueError:
        return None


__all__ = [
    "HELD_PR_MARKER_PREFIX",
    "OWNER_ANSWER_ROUTE",
    "OWNER_KEPT_DECISIONS",
    "OWNER_QUESTION_ROUTE",
    "OWNER_RAISABLE_DECISIONS",
    "OwnerDecision",
    "owner_decision",
    "owner_kept_decision",
]
