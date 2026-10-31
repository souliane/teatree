"""The merged pull request a settled ticket's remainder is judged against."""

from teatree.core.models import MergeAudit, PullRequest, Ticket
from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url


def last_merged_pr(ticket: Ticket) -> tuple[PullRequest, PrRef] | None:
    rows = [
        (row, ref)
        for row in PullRequest.objects.filter(ticket=ticket, state=PullRequest.State.MERGED)
        if (ref := pr_ref_from_url(row.url)) is not None
    ]
    if len(rows) == 1:
        return rows[0]
    if not rows:
        return None

    audits = MergeAudit.objects.filter(clear__pr_id__in=[ref.pr_id for _, ref in rows]).select_related("clear")
    stamped: list[tuple[MergeAudit, PullRequest, PrRef]] = []
    for audit in audits:
        audit_ref = PrRef(
            slug=audit.repo_slug or audit.clear.slug,
            pr_id=audit.clear.pr_id,
            host_kind=audit.clear.host_kind,
        )
        stamped.extend((audit, row, ref) for row, ref in rows if ref.same_pr(audit_ref))
    if {row.pk for _, row, _ in stamped} != {row.pk for row, _ in rows}:
        return None
    _, row, ref = max(stamped, key=lambda entry: (entry[0].merged_at, entry[0].pk))
    return row, ref
