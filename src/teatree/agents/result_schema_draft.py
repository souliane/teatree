"""The ``review_request`` and ``ship_readiness`` envelope fields of the draft-returning phases.

Split out of the over-cap ``result_schema.py``. An executor run holds no forge credential, no
``t3`` and no push, so ``requesting_review`` and ``shipping`` return a draft or a readiness
verdict, and the worker acts on it through the gated seams it already owns
(:mod:`teatree.agents.review_request_recorder`, :mod:`teatree.agents.ship_readiness_recorder`).
"""

from collections.abc import Mapping
from typing import Final, TypedDict

from teatree.core.modelkit.phases import normalize_phase

type JSONSchema = dict[str, object]

#: The field a run on the read-only executor must return, because it can neither request a review nor ship.
EXECUTOR_DRAFT_FIELDS: Final = {"requesting_review": "review_request", "shipping": "ship_readiness"}
EXECUTOR_HARNESS: Final = "codex_app_server"


class ReviewRequestDraft(TypedDict, total=False):
    """A ``requesting_review`` run's verdict on one pull request and the subject it drafted."""

    ready: bool
    pr_url: str
    title: str
    reasons: list[str]


class ShipReadiness(TypedDict, total=False):
    """A ``shipping`` run's readiness verdict; the worker's ship job does the push."""

    ready: bool
    reasons: list[str]


REVIEW_REQUEST_SCHEMA_PROPERTY: JSONSchema = {
    "type": "object",
    "description": (
        "A requesting_review run's draft: whether the pull request is ready for human review and the "
        "one-line subject to send. The worker posts it through review_request_post; `pr_url` only "
        "selects among the ticket's own pull requests."
    ),
    "properties": {
        "ready": {"type": "boolean"},
        "pr_url": {"type": "string"},
        "title": {"type": "string"},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ready"],
}

SHIP_READINESS_SCHEMA_PROPERTY: JSONSchema = {
    "type": "object",
    "description": (
        "A shipping run's readiness verdict. `ready` true lets the worker run the ship gates and queue the "
        "push and pull request; false fails the task and ships nothing, `reasons` naming the blockers."
    ),
    "properties": {
        "ready": {"type": "boolean"},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ready"],
}


def executor_draft_evidence_error(phase: str, result: Mapping[str, object], *, selected_harness: str) -> str:
    """The refusal for an executor run that returned no draft or readiness verdict, else ``""``."""
    field = EXECUTOR_DRAFT_FIELDS.get(normalize_phase(phase))
    if field is None or selected_harness != EXECUTOR_HARNESS or result.get("needs_user_input"):
        return ""
    if isinstance(result.get(field), Mapping):
        return ""
    return f"missing required evidence for phase '{phase}': a read-only executor run must return `{field}`"
