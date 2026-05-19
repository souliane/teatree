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
    """Audit one successful outward review action for the drift verifier."""
    from teatree.outbound_claim import record_claim  # noqa: PLC0415

    record_claim(
        kind=kind,
        idempotency_key=f"{kind}:{repo}!{mr}:{artifact_id}",
        target_url=gitlab_mr_url(base_url_resolver(), repo, mr),
        extra={"repo": repo, "mr": mr, "artifact_id": str(artifact_id), **extra},
    )


__all__ = ["gitlab_mr_url", "record_note_claim"]
