"""Pre-gate-passed publishing bodies for :class:`teatree.cli.review.ReviewService`.

Extracted from :mod:`teatree.cli.review` to keep that file under the
module-health LOC budget (souliane/teatree#1280). The functions here run
*after* the pre-publish gate chain in
:meth:`ReviewService._run_pre_publish_gates` — they never re-check the
gates and they are not part of the public CLI surface.

Signature: each function takes the ``ReviewService`` instance as the
first argument (the methods on the class are thin shims that call these).
SLF001 (private member access) is muted module-wide via per-file-ignores
because this module IS the extracted implementation of those methods.
"""
# ruff: noqa: SLF001 — sibling-module extraction of ReviewService method bodies (#1280).

from http import HTTPStatus
from typing import TYPE_CHECKING

from teatree.cli.review.approval import identity_in_approved_by
from teatree.cli.review.audit import (
    ReviewAfterReceipt,
    notify_review_after_receipt,
    record_note_claim,
    verify_approval_landed,
    verify_bulk_publish,
    verify_discussion_resolved,
    verify_issue_note_deleted,
    verify_note_deleted,
    verify_note_landed,
    verify_unapproval_landed,
)

_HTTP_OK_CODES = frozenset({HTTPStatus.OK, HTTPStatus.CREATED, HTTPStatus.NO_CONTENT})


def _resolve_inline_position(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
    """Indirection so test monkeypatches on ``teatree.cli.review.service.resolve_inline_position`` apply here too."""
    from teatree.cli.review import service as review_mod  # noqa: PLC0415

    return review_mod.resolve_inline_position(*args, **kwargs)


resolve_inline_position = _resolve_inline_position

if TYPE_CHECKING:
    from teatree.cli.review.service import ReviewService


# ast-grep-ignore: ac-django-no-complexity-suppressions
def post_draft_note_impl(  # noqa: PLR0913 — every kwarg maps 1:1 to a public CLI flag on `review post-draft-note`.
    service: "ReviewService",
    repo: str,
    mr: int,
    note: str,
    *,
    file: str,
    line: int,
) -> tuple[str, int]:
    """The pre-gate-passed body of :meth:`ReviewService.post_draft_note`."""
    api = service._get_api()
    encoded = repo.replace("/", "%2F")
    endpoint = f"projects/{encoded}/merge_requests/{mr}/draft_notes"

    if not (file and line):
        result = api.post_json(endpoint, {"note": note})
        if not result:
            return "Failed to post draft note", 1
        note_id = dict(result).get("id")
        verify_note_landed(api, encoded, mr, note_id, endpoint="draft_notes")
        record_note_claim(service._resolve_base_url, repo, mr, note_id, endpoint="draft_notes")
        return f"OK draft_note_id={note_id}", 0

    position, error = resolve_inline_position(api, encoded, mr, file, line)
    if position is None:
        return error, 1

    result = api.post_json(endpoint, {"note": note, "position": position})
    if not result:
        return "Failed to post draft note", 1
    result_dict = dict(result) if isinstance(result, dict) else {}
    note_id = result_dict.get("id")
    line_code = result_dict.get("line_code")
    if line_code:
        verify_note_landed(api, encoded, mr, note_id, endpoint="draft_notes")
        record_note_claim(service._resolve_base_url, repo, mr, note_id, endpoint="draft_notes", file=file, line=line)
        return f"OK draft_note_id={note_id}\nline_code={line_code}", 0

    if isinstance(note_id, int):
        api.delete(f"{endpoint}/{note_id}")
    return (
        f"GitLab refused to anchor the draft on {file}:{line} (line_code came back null). "
        "This usually means the file diff is collapsed because of its size; the draft_notes "
        "API cannot anchor on collapsed-diff files. Workaround: "
        f"`t3 review post-comment {repo} {mr} ... --file {file} --line {line}` "
        "(creates an immediate non-draft inline discussion)."
    ), 1


# ast-grep-ignore: ac-django-no-complexity-suppressions
def post_comment_impl(  # noqa: PLR0913 — every kwarg maps 1:1 to a public CLI flag on `review post-comment`.
    service: "ReviewService",
    repo: str,
    mr: int,
    note: str,
    *,
    file: str,
    line: int,
) -> tuple[str, int]:
    """The pre-gate-passed body of :meth:`ReviewService.post_comment` (live path)."""
    api = service._get_api()
    encoded = repo.replace("/", "%2F")

    if not (file and line):
        result = api.post_json(f"projects/{encoded}/merge_requests/{mr}/notes", {"body": note})
        if not result:
            return "Failed to post comment", 1
        result_dict = dict(result) if isinstance(result, dict) else {}
        note_id = result_dict.get("id")
        verify_note_landed(api, encoded, mr, note_id, endpoint="notes")
        record_note_claim(service._resolve_base_url, repo, mr, note_id, endpoint="notes")
        notify_review_after_receipt(
            service._resolve_base_url,
            repo,
            mr,
            review_action=ReviewAfterReceipt(
                action="post_comment",
                summary=f"posted comment note_id={note_id} on {repo}!{mr}",
                note_web_url=str(result_dict.get("web_url", "")),
            ),
        )
        return f"OK note_id={note_id}", 0

    position, error = resolve_inline_position(api, encoded, mr, file, line)
    if position is None:
        return error, 1

    result = api.post_json(
        f"projects/{encoded}/merge_requests/{mr}/discussions",
        {"body": note, "position": position},
    )
    if not result:
        return "Failed to post comment", 1
    result_dict = dict(result) if isinstance(result, dict) else {}
    discussion_id = result_dict.get("id")
    notes = result_dict.get("notes")
    first_note = notes[0] if isinstance(notes, list) and notes else {}
    note_web_url = str(first_note.get("web_url", "")) if isinstance(first_note, dict) else ""
    first_note_id = first_note.get("id") if isinstance(first_note, dict) else None
    verify_note_landed(api, encoded, mr, first_note_id, endpoint="notes")
    # Record the claim BEFORE the anchor verdict so a retry of a degraded post
    # is a no-op against the ledger, not a double-post — the rc=1 below is what
    # stops the caller from claiming an inline anchor that did not happen.
    record_note_claim(service._resolve_base_url, repo, mr, discussion_id, endpoint="discussions", file=file, line=line)
    notify_review_after_receipt(
        service._resolve_base_url,
        repo,
        mr,
        review_action=ReviewAfterReceipt(
            action="post_comment",
            summary=f"posted inline comment discussion_id={discussion_id} on {repo}!{mr}",
            note_web_url=note_web_url,
        ),
    )
    position = first_note.get("position") if isinstance(first_note, dict) else None
    if not (isinstance(position, dict) and position.get("new_path")):
        return (
            f"REFUSED: GitLab silently downgraded the inline post on {file}:{line} to an MR-level "
            f"note (response notes[0].position has no new_path; discussion_id={discussion_id}). The "
            "comment is NOT anchored inline — this usually means the position did not fall on a "
            "+-added or context line in the diff hunk. Re-anchor on a changed line and re-post."
        ), 1
    return f"OK discussion_id={discussion_id} (inline DiffNote)", 0


def publish_draft_notes_impl(service: "ReviewService", repo: str, mr: int, *, encoded: str) -> tuple[str, int]:
    """The pre-gate-passed publish body of :meth:`ReviewService.publish_draft_notes`."""
    api = service._get_api()
    status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/draft_notes/bulk_publish")
    if status in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
        verify_bulk_publish(api, encoded, mr)
        record_note_claim(service._resolve_base_url, repo, mr, "bulk_publish", endpoint="draft_notes/bulk_publish")
        notify_review_after_receipt(
            service._resolve_base_url,
            repo,
            mr,
            review_action=ReviewAfterReceipt(
                action="publish_draft_notes",
                summary=f"published all draft notes on {repo}!{mr}",
            ),
        )
        return "OK — all draft notes published", 0
    return f"Failed: HTTP {status}", 1


# ast-grep-ignore: ac-django-no-complexity-suppressions
def reply_to_discussion_impl(  # noqa: PLR0913 — extracted ReviewService publish body; args mirror the method (#1280).
    service: "ReviewService", repo: str, mr: int, discussion_id: str, body: str, *, encoded: str
) -> tuple[str, int]:
    """The pre-gate-passed publish body of :meth:`ReviewService.reply_to_discussion`."""
    api = service._get_api()
    result = api.post_json(
        f"projects/{encoded}/merge_requests/{mr}/discussions/{discussion_id}/notes",
        {"body": body},
    )
    if not result:
        return "Failed to post reply", 1
    result_dict = dict(result) if isinstance(result, dict) else {}
    note_id = result_dict.get("id")
    verify_note_landed(api, encoded, mr, note_id, endpoint="notes")
    record_note_claim(
        service._resolve_base_url, repo, mr, note_id, endpoint="discussions/notes", discussion_id=discussion_id
    )
    notify_review_after_receipt(
        service._resolve_base_url,
        repo,
        mr,
        review_action=ReviewAfterReceipt(
            action="reply_to_discussion",
            summary=f"replied to discussion {discussion_id} (note_id={note_id}) on {repo}!{mr}",
            note_web_url=str(result_dict.get("web_url", "")),
        ),
    )
    return f"OK reply_note_id={note_id}", 0


# ast-grep-ignore: ac-django-no-complexity-suppressions
def resolve_discussion_impl(  # noqa: PLR0913 — extracted ReviewService publish body; args mirror the method (#1280).
    service: "ReviewService", repo: str, mr: int, discussion_id: str, *, resolved: bool, encoded: str
) -> tuple[str, int]:
    """The pre-gate-passed publish body of :meth:`ReviewService.resolve_discussion`."""
    api = service._get_api()
    flag = "true" if resolved else "false"
    status = api.put_status(f"projects/{encoded}/merge_requests/{mr}/discussions/{discussion_id}?resolved={flag}")
    if status in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
        verify_discussion_resolved(api, encoded, mr, discussion_id, resolved=resolved)
        record_note_claim(
            service._resolve_base_url,
            repo,
            mr,
            f"{discussion_id}#resolved={flag}",
            endpoint="discussions/resolve",
            resolved=resolved,
        )
        notify_review_after_receipt(
            service._resolve_base_url,
            repo,
            mr,
            review_action=ReviewAfterReceipt(
                action="resolve_discussion",
                summary=f"set discussion {discussion_id} resolved={resolved} on {repo}!{mr}",
            ),
        )
        return f"OK resolved={resolved}", 0
    return f"Failed: HTTP {status}", 1


# ast-grep-ignore: ac-django-no-complexity-suppressions
def update_note_impl(  # noqa: PLR0913 — extracted ReviewService publish body; args mirror the method (#1280).
    service: "ReviewService", repo: str, mr: int, note_id: int, body: str, *, encoded: str
) -> tuple[str, int]:
    """The pre-gate-passed publish body of :meth:`ReviewService.update_note` (draft → published fallback)."""
    api = service._get_api()
    draft_status = api.put_status(
        f"projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}",
        {"note": body},
    )
    if draft_status == HTTPStatus.OK:
        verify_note_landed(api, encoded, mr, note_id, endpoint="draft_notes")
        record_note_claim(service._resolve_base_url, repo, mr, f"update:draft:{note_id}", endpoint="draft_notes/update")
        notify_review_after_receipt(
            service._resolve_base_url,
            repo,
            mr,
            review_action=ReviewAfterReceipt(
                action="update_note",
                summary=f"updated draft_note_id={note_id} on {repo}!{mr}",
            ),
        )
        return f"OK updated draft_note_id={note_id}", 0
    if draft_status != HTTPStatus.NOT_FOUND:
        return f"Failed (draft): HTTP {draft_status}", 1

    pub_status = api.put_status(
        f"projects/{encoded}/merge_requests/{mr}/notes/{note_id}",
        {"body": body},
    )
    if pub_status == HTTPStatus.OK:
        verify_note_landed(api, encoded, mr, note_id, endpoint="notes")
        record_note_claim(service._resolve_base_url, repo, mr, f"update:pub:{note_id}", endpoint="notes/update")
        notify_review_after_receipt(
            service._resolve_base_url,
            repo,
            mr,
            review_action=ReviewAfterReceipt(
                action="update_note",
                summary=f"updated published note_id={note_id} on {repo}!{mr}",
            ),
        )
        return f"OK updated note_id={note_id}", 0
    return f"Failed: HTTP {pub_status}", 1


def delete_discussion_impl(
    service: "ReviewService", repo: str, mr: int, note_id: int, *, encoded: str
) -> tuple[str, int]:
    """The pre-gate-passed publish body of :meth:`ReviewService.delete_discussion`."""
    api = service._get_api()
    status = api.delete(f"projects/{encoded}/merge_requests/{mr}/notes/{note_id}")
    if status == HTTPStatus.NO_CONTENT:
        verify_note_deleted(api, encoded, mr, note_id)
        notify_review_after_receipt(
            service._resolve_base_url,
            repo,
            mr,
            review_action=ReviewAfterReceipt(
                action="delete_discussion",
                summary=f"deleted published note_id={note_id} on {repo}!{mr}",
            ),
        )
        return f"OK deleted note_id={note_id}", 0
    return f"Failed: HTTP {status}", status


def delete_issue_note_impl(
    service: "ReviewService", repo: str, issue_iid: int, note_id: int, *, encoded: str
) -> tuple[str, int]:
    """The pre-gate-passed publish body of :meth:`ReviewService.delete_issue_note`.

    The issue/work-item twin of :func:`delete_discussion_impl`: DELETE the
    note off the issue's notes endpoint, then read it back (#2081) so a
    phantom delete cannot be reported as done.
    """
    api = service._get_api()
    status = api.delete(f"projects/{encoded}/issues/{issue_iid}/notes/{note_id}")
    if status == HTTPStatus.NO_CONTENT:
        verify_issue_note_deleted(api, encoded, issue_iid, note_id)
        notify_review_after_receipt(
            service._resolve_base_url,
            repo,
            0,
            review_action=ReviewAfterReceipt(
                action="delete_issue_note",
                summary=f"deleted note {note_id} on issue {repo}#{issue_iid}",
            ),
            issue_iid=issue_iid,
        )
        return f"OK deleted note_id={note_id} on issue #{issue_iid}", 0
    return f"Failed: HTTP {status}", status


def approve_impl(service: "ReviewService", repo: str, mr: int, *, encoded: str) -> tuple[str, int]:
    """The pre-gate-passed publish body of :meth:`ReviewService.approve` (post-review-precondition)."""
    api = service._get_api()
    status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/approve")
    if status in _HTTP_OK_CODES:
        verify_approval_landed(api, encoded, mr)
        record_note_claim(service._resolve_base_url, repo, mr, "approve", kind="gitlab_approve", endpoint="approve")
        return f"OK approved !{mr}", 0
    # GitLab returns 401 for the idempotent already-approved case as well as a
    # genuine auth failure (#1029). Probe /approvals: identity already in
    # approved_by → no-op success.
    if identity_in_approved_by(api, encoded, mr):
        return f"Already approved by {api.current_username()} (!{mr})", 0
    return f"Failed: HTTP {status}", 1


def unapprove_impl(service: "ReviewService", repo: str, mr: int, *, encoded: str) -> tuple[str, int]:
    """The pre-gate-passed publish body of :meth:`ReviewService.unapprove`."""
    api = service._get_api()
    status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/unapprove")
    if status in _HTTP_OK_CODES:
        verify_unapproval_landed(api, encoded, mr)
        record_note_claim(service._resolve_base_url, repo, mr, "unapprove", kind="gitlab_approve", endpoint="unapprove")
        return f"OK unapproved !{mr}", 0
    return f"Failed: HTTP {status}", 1
