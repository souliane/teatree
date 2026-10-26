"""Typed result property for a reviewer whose checkout missed the PR head."""

from typing import TypedDict


class ReviewHeadMismatch(TypedDict):
    observed_sha: str
    reason: str


REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY = {
    "type": "object",
    "properties": {
        "observed_sha": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["observed_sha", "reason"],
    "additionalProperties": False,
}
