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
from http import HTTPStatus

import httpx

from teatree.cli.review.approval import identity_in_approved_by


class ReviewArtifactNotVerifiedError(RuntimeError):
    """The post dispatched but a read-back could not confirm the artifact landed (#2081).

    Raised by :func:`verify_review_artifact` ONLY on a *definite* "artifact is
    not there" signal (a 404 on the read-back GET, or an approval whose
    ``approved_by`` does not contain the posting identity). A non-404 transport
    error (5xx, timeout, connection failure) is NOT this — it re-raises the raw
    ``httpx`` error so a flaky GET never turns a genuinely successful post into
    a phantom failure (the inverse of the incident).

    Raised *inside* the ``publish`` body so it propagates through
    :func:`teatree.cli.review.on_behalf.publish_on_behalf` and rolls back the
    on-behalf approval consume + audit (#1879) — exactly like a post failure.
    """


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


def _gitlab_issue_url(base_url: str, repo: str, issue_iid: int) -> str:
    """Web URL for the issue/work-item (receipt link; not the API endpoint)."""
    web_root = base_url.rstrip("/").removesuffix("/api/v4")
    return f"{web_root}/{repo}/-/issues/{issue_iid}"


def verify_note_landed(api: object, encoded: str, mr: int, artifact_id: object, *, endpoint: str) -> None:
    """Read back one posted note/draft note by id; raise if GitLab says it is gone (#2081).

    The inline twin of ``gitlab_note_verifier_for_overlay``: GET
    ``projects/{enc}/merge_requests/{mr}/{notes|draft_notes}/{id}`` and treat a
    404 as a confirmed-missing artifact (raise
    :class:`ReviewArtifactNotVerifiedError`). Any other transport error
    re-raises unchanged so a flaky GET never becomes a phantom failure. A
    non-integer ``artifact_id`` (no id to read back) is accepted as verified —
    there is nothing to confirm, matching the delayed verifier's contract.
    """
    aid = str(artifact_id)
    if not aid.isdigit():
        return
    sub = "draft_notes" if "draft_notes" in endpoint else "notes"
    try:
        result = api.get_json(f"projects/{encoded}/merge_requests/{mr}/{sub}/{aid}")  # type: ignore[attr-defined]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == HTTPStatus.NOT_FOUND:
            msg = f"GitLab {sub[:-1].replace('_', ' ')} {aid} not found on !{mr} after post — not reporting as posted"
            raise ReviewArtifactNotVerifiedError(msg) from exc
        raise
    if result is None:
        msg = f"GitLab {sub[:-1].replace('_', ' ')} {aid} read-back returned no payload on !{mr}"
        raise ReviewArtifactNotVerifiedError(msg)


def verify_note_deleted(api: object, encoded: str, mr: int, note_id: object) -> None:
    """Confirm a deleted note is actually gone (#2081) — the inverse of :func:`verify_note_landed`.

    GET the note: a 404 confirms the delete took. A 200 (note still present)
    → :class:`ReviewArtifactNotVerifiedError`. Any other transport error
    re-raises unchanged (transient, not a failed delete).
    """
    aid = str(note_id)
    if not aid.isdigit():
        return
    try:
        api.get_json(f"projects/{encoded}/merge_requests/{mr}/notes/{aid}")  # type: ignore[attr-defined]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == HTTPStatus.NOT_FOUND:
            return
        raise
    msg = f"note {aid} still present on !{mr} after delete — not reporting as deleted"
    raise ReviewArtifactNotVerifiedError(msg)


def verify_issue_note_deleted(api: object, encoded: str, issue_iid: int, note_id: object) -> None:
    """Confirm a deleted ISSUE/work-item note is actually gone — the issue twin of :func:`verify_note_deleted`.

    GET the note: a 404 confirms the delete took. A 200 (note still present)
    → :class:`ReviewArtifactNotVerifiedError`. Any other transport error
    re-raises unchanged (transient, not a failed delete).
    """
    aid = str(note_id)
    if not aid.isdigit():
        return
    try:
        api.get_json(f"projects/{encoded}/issues/{issue_iid}/notes/{aid}")  # type: ignore[attr-defined]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == HTTPStatus.NOT_FOUND:
            return
        raise
    msg = f"note {aid} still present on issue #{issue_iid} after delete — not reporting as deleted"
    raise ReviewArtifactNotVerifiedError(msg)


def verify_bulk_publish(api: object, encoded: str, mr: int) -> None:
    """Confirm a bulk-publish actually flushed the drafts (#2081 incident's missed signal).

    The incident: ``draft_notes/bulk_publish`` returned 200 yet ZERO notes
    landed. Confirm by listing the MR's remaining draft notes — after a
    successful publish there must be none — and that at least one authored note
    now exists. A non-empty draft list (or no authored notes) means the publish
    did not take; raise :class:`ReviewArtifactNotVerifiedError`. Transport
    errors propagate unchanged (transient, not a failed post).
    """
    drafts = api.get_json(f"projects/{encoded}/merge_requests/{mr}/draft_notes")  # type: ignore[attr-defined]
    if isinstance(drafts, list) and drafts:
        msg = f"bulk publish reported OK but {len(drafts)} draft note(s) remain on !{mr} — not reporting as published"
        raise ReviewArtifactNotVerifiedError(msg)
    notes = api.get_json(f"projects/{encoded}/merge_requests/{mr}/notes")  # type: ignore[attr-defined]
    if not (isinstance(notes, list) and notes):
        msg = f"bulk publish reported OK but no authored notes are present on !{mr} — not reporting as published"
        raise ReviewArtifactNotVerifiedError(msg)


def verify_discussion_resolved(api: object, encoded: str, mr: int, discussion_id: str, *, resolved: bool) -> None:
    """Read back a discussion after a resolve flip; raise if the state did not take (#2081).

    GET ``projects/{enc}/merge_requests/{mr}/discussions/{id}`` and confirm its
    resolvable notes carry the requested ``resolved`` flag. A 404 (discussion
    gone) or a mismatched flag → :class:`ReviewArtifactNotVerifiedError`. Any
    other transport error re-raises unchanged (transient, not a failed flip).
    """
    try:
        discussion = api.get_json(f"projects/{encoded}/merge_requests/{mr}/discussions/{discussion_id}")  # type: ignore[attr-defined]
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == HTTPStatus.NOT_FOUND:
            msg = f"discussion {discussion_id} not found on !{mr} after resolve flip — not reporting as resolved"
            raise ReviewArtifactNotVerifiedError(msg) from exc
        raise
    notes = discussion.get("notes") if isinstance(discussion, dict) else None
    resolvable = [n for n in notes if isinstance(n, dict) and n.get("resolvable")] if isinstance(notes, list) else []
    if resolvable and all(bool(n.get("resolved")) == resolved for n in resolvable):
        return
    msg = f"discussion {discussion_id} resolved!={resolved} on !{mr} after flip — not reporting as resolved"
    raise ReviewArtifactNotVerifiedError(msg)


def verify_approval_landed(api: object, encoded: str, mr: int) -> None:
    """Confirm the posting identity is in the MR's ``approved_by`` after an approve (#2081).

    Reuses :func:`identity_in_approved_by` (same GET shape as
    ``gitlab_approve_verifier_for_overlay``). Identity absent → raise
    :class:`ReviewArtifactNotVerifiedError`. A transport error inside
    ``identity_in_approved_by`` propagates unchanged (transient, not a failed
    approve).
    """
    if not identity_in_approved_by(api, encoded, mr):  # type: ignore[arg-type]
        msg = f"approval not present in approved_by on !{mr} after approve — not reporting as approved"
        raise ReviewArtifactNotVerifiedError(msg)


def verify_unapproval_landed(api: object, encoded: str, mr: int) -> None:
    """Confirm the posting identity is NOT in ``approved_by`` after an unapprove (#2081).

    The inverse of :func:`verify_approval_landed` — mirrors the
    ``endpoint == "unapprove"`` branch of ``gitlab_approve_verifier_for_overlay``.
    Identity still present → raise :class:`ReviewArtifactNotVerifiedError`.
    A transport error propagates unchanged (transient, not a failed unapprove).
    """
    if identity_in_approved_by(api, encoded, mr):  # type: ignore[arg-type]
        msg = f"approval still present in approved_by on !{mr} after unapprove — not reporting as unapproved"
        raise ReviewArtifactNotVerifiedError(msg)


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
    from teatree.outbound_claim import record_claim  # noqa: PLC0415 — deferred: keeps CLI startup light

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
    issue_iid: int | None = None,
) -> None:
    """Fire the #949 after-receipt visibility DM for a published review action.

    Kept here (next to ``record_note_claim``) so :mod:`teatree.cli.review`
    stays under the module-health LOC cap and each ``ReviewService``
    method adds a single call. ``review_action.note_web_url`` is the
    GitLab note's ``web_url`` from the API response when available;
    otherwise the canonical URL is used so the post is always reported.
    When ``issue_iid`` is given the receipt targets the issue/work-item
    (``<repo>#<iid>`` + the issue web URL) instead of an MR — one path
    serves both surfaces. Never raises — ``notify_user_on_behalf_post``
    records the DM outcome durably.
    """
    from teatree.core.on_behalf_post_receipt import notify_user_on_behalf_post  # noqa: PLC0415 — lazy CLI import

    if issue_iid is not None:
        target = f"{repo}#{issue_iid}"
        canonical = _gitlab_issue_url(base_url_resolver(), repo, issue_iid)
    else:
        target = f"{repo}!{mr}"
        canonical = gitlab_mr_url(base_url_resolver(), repo, mr)
    notify_user_on_behalf_post(
        target=target,
        action=review_action.action,
        destination=target,
        artifact_url=review_action.note_web_url or canonical,
        summary=review_action.summary,
    )


__all__ = [
    "ReviewAfterReceipt",
    "ReviewArtifactNotVerifiedError",
    "gitlab_mr_url",
    "notify_review_after_receipt",
    "record_note_claim",
    "verify_approval_landed",
    "verify_bulk_publish",
    "verify_discussion_resolved",
    "verify_issue_note_deleted",
    "verify_note_deleted",
    "verify_note_landed",
    "verify_unapproval_landed",
]
