from typing import TYPE_CHECKING, cast

from django.utils import timezone

from teatree.core.models.checks_admission import ReviewVerdictError
from teatree.core.models.finding_standard import Finding

if TYPE_CHECKING:
    from teatree.core.models.review_verdict import ReviewVerdict


def refresh_duplicate_verdict(
    recorded: "ReviewVerdict", findings: list[Finding], gaps: list[str], verify_result: str
) -> None:
    if not isinstance(recorded.findings, list):
        msg = f"verdict {recorded.pk} findings is not a list"
        raise ReviewVerdictError(msg)
    additions = [finding.as_dict() for finding in findings]
    merged = [*recorded.findings, *(finding for finding in additions if finding not in recorded.findings)]
    update_fields = []
    if merged != recorded.findings:
        recorded.findings = merged
        update_fields.append("findings")
    if recorded.verdict == recorded.Verdict.UNVERIFIED:
        if recorded.evidence_gaps != gaps:
            current_gaps = cast("list[str]", recorded.evidence_gaps)
            current_gaps[:] = gaps
            update_fields.append("evidence_gaps")
        if recorded.gh_verify_result != verify_result:
            recorded.gh_verify_result = verify_result
            update_fields.append("gh_verify_result")
        if update_fields:
            recorded.recorded_at = timezone.now()
            update_fields.append("recorded_at")
    if update_fields:
        recorded.save(update_fields=update_fields)
