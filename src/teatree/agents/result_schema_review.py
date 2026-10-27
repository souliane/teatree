"""Typed result property for a reviewer whose checkout missed the PR head."""

from typing import TypedDict

from teatree.core.modelkit.review_contract import ENVELOPE_FINDINGS_RULE


class ReviewHeadMismatch(TypedDict):
    observed_sha: str
    reason: str


class ReviewVenueLimit(TypedDict):
    limits: list[str]


REVIEW_VERDICT_SCHEMA_PROPERTY = {
    "type": "object",
    "description": "A reviewing-phase agent's typed verdict, recorded server-side (corr-11).",
    "properties": {
        "verdict": {"type": "string", "enum": ["merge_safe", "hold"]},
        "reviewed_sha": {
            "type": "string",
            "description": (
                "Full 40-char hex SHA the review bound to. REQUIRED: an undisclosed head is "
                "refused, never read as agreement with the dispatch head (#4168)."
            ),
        },
        "reviewer_identity": {"type": "string"},
        "gh_verify_result": {"type": "string", "enum": ["green", "pending", "failed"]},
        "blast_class": {"type": "string", "enum": ["substrate", "logic", "docs"]},
        "findings": {
            "type": "array",
            "description": ENVELOPE_FINDINGS_RULE,
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string"},
                    "summary": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                },
            },
        },
        "merge_result_retake": {
            "type": "boolean",
            "description": (
                "Attests that every finding citing a file outside the PR's changed-file set was "
                "re-measured on the materialised MERGE RESULT (`t3 review merge-tree`), not the "
                "branch checkout alone. Without it such a finding cannot carry blocking severity "
                "and the whole verdict is refused (#4251)."
            ),
        },
        "rubric_grades": {
            "type": "array",
            "description": (
                "Your grade of EVERY criterion on the reviewed ticket's rubric — you are the "
                "independent verifier the done-gate requires. A verdict leaving any criterion "
                "ungraded is refused and records nothing. NOT in `required`: whether the ticket "
                "HAS a rubric is a DB question only the recorder can answer."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "ordinal": {"type": "integer"},
                    "text_hash": {
                        "type": "string",
                        "description": "Hash shown beside the criterion being graded.",
                    },
                    "status": {"type": "string", "enum": ["pass", "fail"]},
                    "rationale": {
                        "type": "string",
                        "description": (
                            "What proves the criterion — any test kind, free-form prose. "
                            "REQUIRED on a pass; a fail needs none."
                        ),
                    },
                },
                "required": ["ordinal", "status"],
            },
        },
    },
    "required": ["verdict", "reviewed_sha"],
}

REVIEW_HEAD_MISMATCH_SCHEMA_PROPERTY = {
    "type": "object",
    "properties": {
        "observed_sha": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["observed_sha", "reason"],
    "additionalProperties": False,
}

REVIEW_VENUE_LIMIT_SCHEMA_PROPERTY = {
    "type": "object",
    "properties": {"limits": {"type": "array", "items": {"type": "string"}}},
    "required": ["limits"],
    "additionalProperties": False,
}
