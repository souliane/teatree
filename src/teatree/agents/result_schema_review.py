"""Typed result property for a reviewer whose checkout missed the PR head."""

from typing import TypedDict

from teatree.core.models.types import RubricGrade


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
