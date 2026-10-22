"""Publish NITs and stamp the exact points whose forge writes read back."""

from collections.abc import Iterable
from typing import TYPE_CHECKING

from teatree.core.backend_registry import get_backend_provider
from teatree.core.models.finding_standard import FindingKind
from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.pull_request import PullRequest
from teatree.core.overlay_loader import get_overlay_for_ticket
from teatree.core.review.nit_lane import NitLaneError, NitPoint, nit_point_marker, track_nits
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models.critic_verdict import CriticItemVerdict
    from teatree.core.models.review_verdict import Finding
    from teatree.core.models.ticket import Ticket


_RECEIPTS_KEY = "nit_lane_receipts"


def review_nits(findings: Iterable["Finding"]) -> tuple[NitPoint, ...]:
    return tuple(
        dict.fromkeys(
            NitPoint(finding.file, finding.line, finding.summary)
            for finding in findings
            if finding.kind == FindingKind.NIT
        )
    )


def critic_nits(items: Iterable["CriticItemVerdict"]) -> tuple[NitPoint, ...]:
    return tuple(
        dict.fromkeys(
            NitPoint(point.file, point.line, point.summary)
            for item in items
            for point in item.points
            if point.kind == FindingKind.NIT
        )
    )


def pr_url_for_nits(ticket: "Ticket", head_sha: str) -> str:
    urls = {
        url for url in PullRequest.objects.filter(ticket=ticket).values_list("url", flat=True) if pr_ref_from_url(url)
    }
    if len(urls) == 1:
        return urls.pop()
    if not urls and pr_ref_from_url(ticket.issue_url):
        return ticket.issue_url
    clears = MergeClear.objects.filter(ticket=ticket, reviewed_sha=head_sha).order_by("-pk")
    refs = {(row.slug.casefold(), row.pr_id, row.host_kind) for row in clears}
    if len(refs) != 1:
        return ""
    slug, pr_id, host_kind = refs.pop()
    return PullRequest.objects.recorded_pr_url(slug=slug, pr_id=pr_id, host_kind=host_kind)


def has_nit_receipts(ticket: "Ticket", pr_url: str, head_sha: str, points: tuple[NitPoint, ...]) -> bool:
    if not points:
        return True
    extra = ticket.extra if isinstance(ticket.extra, dict) else {}
    receipts = extra.get(_RECEIPTS_KEY)
    if not isinstance(receipts, dict):
        return False
    for point in points:
        receipt = receipts.get(nit_point_marker(pr_url, head_sha, point))
        if (
            not isinstance(receipt, dict)
            or receipt.get("head_sha") != head_sha
            or not receipt.get("issue_url")
            or not receipt.get("thread_id")
        ):
            return False
    return True


def publish_nits(ticket: "Ticket", pr_url: str, head_sha: str, points: tuple[NitPoint, ...]) -> None:
    if has_nit_receipts(ticket, pr_url, head_sha, points):
        return
    if not pr_url or pr_ref_from_url(pr_url) is None:
        message = "NIT pull request URL is missing or unrecognized"
        raise NitLaneError(message)
    host = get_backend_provider().get_code_host_for_url(get_overlay_for_ticket(ticket), pr_url)
    if host is None:
        message = "NIT pull request host is unavailable"
        raise NitLaneError(message)
    receipt = track_nits(host=host, pr_url=pr_url, head_sha=head_sha, points=points)
    if receipt.head_sha != head_sha or len(receipt.thread_ids) != len(points):
        message = "NIT publisher returned a receipt for a different head or point count"
        raise NitLaneError(message)
    ticket.merge_extra(
        merge_into_dicts={
            _RECEIPTS_KEY: {
                nit_point_marker(pr_url, head_sha, point): {
                    "head_sha": head_sha,
                    "issue_url": receipt.issue_url,
                    "thread_id": thread_id,
                }
                for point, thread_id in zip(points, receipt.thread_ids, strict=True)
            }
        }
    )
