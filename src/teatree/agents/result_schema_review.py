"""Typed result property for a reviewer whose checkout missed the PR head."""

from typing import TypedDict

from teatree.core.modelkit.review_contract import ENVELOPE_FINDINGS_RULE
from teatree.core.models.review_hold_disposition import HoldPointResponse
from teatree.core.models.types import RubricGrade


class ReviewHeadMismatch(TypedDict):
    observed_sha: str
    reason: str


class ReviewVenueLimit(TypedDict):
    limits: list[str]


HOLD_DISPOSITIONS_SCHEMA_PROPERTY = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "hold_id": {"type": "integer"},
            "points": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "classification": {"type": "string", "enum": ["venue_limit", "disproved_premise"]},
                        "citation": {"type": "string"},
                    },
                    "required": ["index", "classification", "citation"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["hold_id", "points"],
        "additionalProperties": False,
    },
}

REVIEW_VERDICT_SCHEMA_PROPERTY = {
    "type": "object",
    "description": "A reviewing-phase agent's typed verdict, recorded server-side (corr-11).",
    "properties": {
        "verdict": {"type": "string", "enum": ["merge_safe", "hold", "unverified"]},
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
                    "kind": {"type": "string", "enum": ["blocking", "nit", "note"]},
                    "basis": {"type": "string", "enum": ["executed", "documentation", "venue_limit"]},
                    "citation": {"type": "string"},
                    "reproduction": {"type": "string"},
                },
            },
        },
        "evidence_gaps": {
            "type": "array",
            "items": {"type": "string", "enum": ["ci_unreadable", "tests_unavailable", "plan_unavailable"]},
        },
        "hold_dispositions": HOLD_DISPOSITIONS_SCHEMA_PROPERTY,
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


class ReviewFinding(TypedDict, total=False):
    severity: str
    summary: str
    file: str
    line: int
    kind: str
    basis: str
    citation: str
    reproduction: str


class ReviewVerdictEnvelope(TypedDict, total=False):
    """A reviewing-phase agent's typed verdict, recorded server-side (corr-11)."""

    verdict: str
    reviewed_sha: str
    reviewer_identity: str
    gh_verify_result: str
    blast_class: str
    findings: list[ReviewFinding]
    evidence_gaps: list[str]
    merge_result_retake: bool
    rubric_grades: list[RubricGrade]
    hold_dispositions: list["HoldDispositionResponse"]


class HoldDispositionResponse(TypedDict):
    hold_id: int
    points: list[HoldPointResponse]


def review_envelope_shape_error(envelope: ReviewVerdictEnvelope) -> str:
    findings = envelope.get("findings", [])
    if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
        return "findings must be a list of objects"
    gaps = envelope.get("evidence_gaps", [])
    if not isinstance(gaps, list) or any(not isinstance(item, str) for item in gaps):
        return "evidence_gaps must be a list of strings"
    responses = envelope.get("hold_dispositions", [])
    if not isinstance(responses, list):
        return "hold_dispositions must be a list of objects"
    for response in responses:
        if not isinstance(response, dict) or type(response.get("hold_id")) is not int:
            return "each hold_dispositions item needs an integer hold_id"
        points = response.get("points")
        if not isinstance(points, list) or any(
            not isinstance(point, dict)
            or type(point.get("index")) is not int
            or not isinstance(point.get("classification"), str)
            or not isinstance(point.get("citation"), str)
            for point in points
        ):
            return "each hold_dispositions item needs points with index, classification, and citation"
    return ""


REVIEW_VENUE_LIMIT_SCHEMA_PROPERTY = {
    "type": "object",
    "properties": {"limits": {"type": "array", "items": {"type": "string"}}},
    "required": ["limits"],
    "additionalProperties": False,
}
