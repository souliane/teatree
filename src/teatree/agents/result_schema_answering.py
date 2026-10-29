"""The envelope fields a shell-denied answering agent hands back: its reply, its work item, its proposed actions.

Split out of the over-cap ``result_schema.py`` (docs/module-health.md), not for a concern of its own.
"""

from typing import TypedDict

type JSONSchema = dict[str, object]


class AnswerEnvelope(TypedDict, total=False):
    """A shell-denied answering agent's drafted reply, handed back for approval (#9)."""

    text: str
    thread_ref: str


class WorkItemEnvelope(TypedDict, total=False):
    """What a shell-denied answering agent decided the owner's request BECOMES (#4527).

    Exactly one of the three shapes: attach to ``existing_issue_url`` (reuse before
    file), file ``title``/``body``, or declare ``no_work_reason``. An absent channel on
    a work-implying request is the silent drop the phase gate refuses.
    """

    title: str
    body: str
    existing_issue_url: str
    no_work_reason: str


class CoordinatorActionRequest(TypedDict, total=False):
    """One action the coordinator proposes; code checks it against the closed table and the owner's limits (#5685)."""

    kind: str
    ticket: str


ANSWER_SCHEMA_PROPERTY: JSONSchema = {
    "type": "object",
    "description": "A shell-denied answering agent's drafted reply, handed back for approval-gated posting.",
    "properties": {
        "text": {"type": "string"},
        "thread_ref": {"type": "string"},
    },
    "required": ["text"],
}

WORK_ITEM_SCHEMA_PROPERTY: JSONSchema = {
    "type": "object",
    "description": (
        "What the owner's request becomes: an existing issue to attach to, a new issue to file, "
        "or a stated reason no work is needed."
    ),
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "existing_issue_url": {"type": "string"},
        "no_work_reason": {"type": "string"},
    },
}

ACTIONS_SCHEMA_PROPERTY: JSONSchema = {
    "type": "array",
    "description": (
        "Actions the coordinator proposes beyond its reply. Code runs only the kinds of the closed action "
        "table, each checked against the owner's limits; anything else is refused and runs nothing."
    ),
    "items": {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "ticket": {"type": "string"},
        },
        "required": ["kind"],
    },
}
