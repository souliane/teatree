"""Claim-ledger plumbing for the review CLI (#1019 outbound_audit).

Kept out of :mod:`teatree.cli.review` so that file stays under the
module-health LOC cap. ``record_note_claim`` is the single call the
review CLI uses to audit one outbound artifact (a GitLab note, a draft
note, an approval/unapproval, etc.) for the
``loop.scanners.outbound_audit`` drift verifier.

Every audit write is best-effort: ``record_claim`` swallows
``IntegrityError`` / ``DatabaseError`` so an outage of the ledger
never breaks the CLI turn that just succeeded.
"""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReviewAfterReceipt:
    """The #949 after-receipt payload for one published review action.

    ``action`` is the ``ReviewService`` method name (the on-behalf-gate
    scope), ``summary`` the one-line description, ``note_web_url`` the
    GitLab note ``web_url`` from the API response when present (else the
    canonical MR URL is the fallback link).
    """

    action: str
    summary: str
    note_web_url: str = ""


def gitlab_mr_url(base_url: str, repo: str, mr: int) -> str:
    """Web URL for the MR (claim ledger target_url; not the API endpoint)."""
    web_root = base_url.rstrip("/").removesuffix("/api/v4")
    return f"{web_root}/{repo}/-/merge_requests/{mr}"


def record_note_claim(
    base_url_resolver: Callable[[], str],
    repo: str,
    mr: int,
    artifact_id: object,
    *,
    kind: str = "gitlab_note",
    **extra: str | int | bool,
) -> None:
    """Audit one successful outward review action for the drift verifier.

    ``record_claim`` stamps ``extra["overlay"]`` from ``T3_OVERLAY_NAME``
    so the audit scanner re-reads the artifact through the same overlay's
    credentials that posted it (#1275).
    """
    from teatree.outbound_claim import record_claim  # noqa: PLC0415

    record_claim(
        kind=kind,
        idempotency_key=f"{kind}:{repo}!{mr}:{artifact_id}",
        target_url=gitlab_mr_url(base_url_resolver(), repo, mr),
        extra={"repo": repo, "mr": mr, "artifact_id": str(artifact_id), **extra},
    )


def notify_review_after_receipt(
    base_url_resolver: Callable[[], str],
    repo: str,
    mr: int,
    *,
    review_action: ReviewAfterReceipt,
) -> None:
    """Fire the #949 after-receipt visibility DM for a published review action.

    Kept here (next to ``record_note_claim``) so :mod:`teatree.cli.review`
    stays under the module-health LOC cap and each ``ReviewService``
    method adds a single call. ``review_action.note_web_url`` is the
    GitLab note's ``web_url`` from the API response when available;
    otherwise the canonical MR URL is used so the post is always
    reported. Never raises — ``notify_user_on_behalf_post`` records the
    DM outcome durably.
    """
    from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post  # noqa: PLC0415

    canonical = gitlab_mr_url(base_url_resolver(), repo, mr)
    notify_user_on_behalf_post(
        target=f"{repo}!{mr}",
        action=review_action.action,
        destination=f"{repo}!{mr}",
        artifact_url=review_action.note_web_url or canonical,
        summary=review_action.summary,
    )


__all__ = [
    "ReviewAfterReceipt",
    "gitlab_mr_url",
    "notify_review_after_receipt",
    "record_note_claim",
]
