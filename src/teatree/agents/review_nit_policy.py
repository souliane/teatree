"""Normalize returned review findings and keep advisory points out of HOLDs."""

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from teatree.agents.result_schema import ReviewFinding, ReviewVerdictEnvelope
from teatree.core.models.review_verdict import Finding, ReviewVerdict
from teatree.core.review.finding_standard import FindingKind

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def normalize_review_envelope(envelope: ReviewVerdictEnvelope, ticket: "Ticket | None") -> ReviewVerdictEnvelope:
    raw_findings = envelope.get("findings")
    if not isinstance(raw_findings, list):
        return envelope
    nit_follow_up = bool(ticket and isinstance(ticket.extra, dict) and ticket.extra.get("nit_follow_up") is True)
    findings = [Finding.from_dict(row) for row in raw_findings if isinstance(row, dict)]
    if nit_follow_up:
        findings = [
            replace(finding, kind=FindingKind.BLOCKING, basis="follow_up")
            if finding.kind == FindingKind.NIT
            else finding
            for finding in findings
        ]
    normalized: ReviewVerdictEnvelope = {
        **envelope,
        "findings": [cast("ReviewFinding", finding.as_dict()) for finding in findings],
    }
    requested = str(envelope.get("verdict", "")).strip().lower()
    blocking = any(finding.kind == FindingKind.BLOCKING for finding in findings)
    if blocking and requested == ReviewVerdict.Verdict.MERGE_SAFE:
        normalized["verdict"] = ReviewVerdict.Verdict.HOLD
    elif (
        findings
        and all(finding.kind == FindingKind.NIT for finding in findings)
        and requested == ReviewVerdict.Verdict.HOLD
        and str(envelope.get("gh_verify_result") or "green").strip().lower() == "green"
    ):
        normalized["verdict"] = ReviewVerdict.Verdict.MERGE_SAFE
    return normalized
