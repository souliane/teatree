"""Normalize returned review findings and keep advisory points out of HOLDs."""

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from teatree.agents.result_schema import ReviewFinding, ReviewVerdictEnvelope
from teatree.core.models.review_target import ReviewTarget
from teatree.core.models.review_verdict import Finding, ReviewVerdict
from teatree.core.review.finding_standard import FindingKind
from teatree.core.review.nit_lane import NitLaneError
from teatree.core.review.nit_publish import pr_url_for_nits, publish_nits, review_nits

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket


def normalize_review_envelope(envelope: ReviewVerdictEnvelope, ticket: "Ticket | None") -> ReviewVerdictEnvelope:
    raw_findings = envelope.get("findings")
    if not isinstance(raw_findings, list):
        return envelope
    nit_follow_up = bool(ticket and isinstance(ticket.extra, dict) and ticket.extra.get("nit_follow_up") is True)
    findings = [Finding.from_dict(row) for row in raw_findings if isinstance(row, dict)]
    if nit_follow_up:
        findings = [replace(finding, kind=FindingKind.BLOCKING) for finding in findings]
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
        and not blocking
        and requested == ReviewVerdict.Verdict.HOLD
        and str(envelope.get("gh_verify_result") or "green").strip().lower() == "green"
    ):
        normalized["verdict"] = ReviewVerdict.Verdict.MERGE_SAFE
    return normalized


def publish_review_nits_with_recovery(ticket: "Ticket", target: ReviewTarget, findings: list[Finding]) -> str:
    if not (points := review_nits(findings)):
        return ""
    ticket.merge_extra(merge_into_dicts={"nit_publication": {target.head_sha: {"status": "pending"}}})
    try:
        pr_url = _nit_pr_url(ticket, target.head_sha)
        publish_nits(ticket, pr_url, target.head_sha, points)
    except NitLaneError as exc:
        ticket.merge_extra(
            merge_into_dicts={"nit_publication": {target.head_sha: {"status": "failed", "error": str(exc)}}}
        )
        return f"review NIT publication refused: {exc}"
    ticket.merge_extra(merge_into_dicts={"nit_publication": {target.head_sha: {"status": "published"}}})
    return ""


def _nit_pr_url(ticket: "Ticket", head_sha: str) -> str:
    pr_url = pr_url_for_nits(ticket, head_sha)
    if not pr_url:
        message = "review NIT has no attributable pull request URL"
        raise NitLaneError(message)
    return pr_url
