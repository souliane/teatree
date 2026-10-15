"""Durable identity for actions requested through an owner question."""

import hashlib
from enum import StrEnum
from typing import NotRequired, TypedDict


class OwnerActionKind(StrEnum):
    ATTEST = "attest"
    VOID = "void"


class ActionRequest(TypedDict):
    kind: str
    target: str
    binding: str
    note: NotRequired[str]


MARKER_PREFIX = "owner-action:"


def action_marker(action: ActionRequest) -> str:
    identity = "\0".join((action["kind"], action["target"], action["binding"]))
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
    if "note" in action and not isinstance(action["note"], str):
        return None
    return action
