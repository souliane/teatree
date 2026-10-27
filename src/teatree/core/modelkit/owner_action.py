"""Durable identity for actions requested through an owner question."""

import hashlib
from enum import StrEnum
from typing import NotRequired, TypedDict

from teatree.core.modelkit.escape_scope import ESCAPE_SCOPE_FIELDS


class OwnerActionKind(StrEnum):
    ATTEST = "attest"
    ESCAPE = "escape"
    VOID = "void"
    MERGE_ITEM_WAIVER = "merge_item_waiver"


class ActionRequest(TypedDict):
    kind: str
    target: str
    binding: str
    note: NotRequired[str]
    host_evidence: NotRequired[bool]
    task_id: NotRequired[int]
    scope: NotRequired[dict[str, str]]


MARKER_PREFIX = "owner-action:"
ATTEST_MARKER_PREFIX = f"{MARKER_PREFIX}{OwnerActionKind.ATTEST}:"


def action_marker(action: ActionRequest) -> str:
    parts = [action["kind"], action["target"], action["binding"]]
    if "host_evidence" in action and action["kind"] != OwnerActionKind.ATTEST:
        parts.append(f"host_evidence:{action['host_evidence']}")
    if "task_id" in action:
        parts.append(f"task:{action['task_id']}")
    identity = "\0".join(parts)
    digest = hashlib.sha256(identity.encode()).hexdigest()[:40]
    return f"{MARKER_PREFIX}{action['kind']}:{digest}"


def is_owner_action_marker(marker: str) -> bool:
    return marker.startswith(MARKER_PREFIX)


def read_action(evidence: object) -> ActionRequest | None:
    if not isinstance(evidence, dict) or not isinstance(action := evidence.get("action"), dict):
        return None
    if action.get("kind") not in OwnerActionKind:
        return None
    if not all(isinstance(action.get(key), str) and action[key] for key in ("target", "binding")):
        return None
    if ("note" in action and not isinstance(action["note"], str)) or (
        "host_evidence" in action and not isinstance(action["host_evidence"], bool)
    ):
        return None
    scope = action.get("scope")
    scope_valid = "scope" not in action
    if action["kind"] == OwnerActionKind.ESCAPE:
        scope_valid = (
            isinstance(scope, dict)
            and set(scope) == ESCAPE_SCOPE_FIELDS
            and all(isinstance(value, str) for value in scope.values())
        )
    if ("task_id" in action and (type(action["task_id"]) is not int or action["task_id"] < 1)) or not scope_valid:
        return None
    return action
