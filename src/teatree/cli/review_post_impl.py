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

from typing import TYPE_CHECKING

from teatree.cli.review_audit import ReviewAfterReceipt, notify_review_after_receipt, record_note_claim


def _resolve_inline_position(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
    """Indirection so test monkeypatches on ``teatree.cli.review.resolve_inline_position`` apply here too."""
    from teatree.cli import review as review_mod  # noqa: PLC0415

    return review_mod.resolve_inline_position(*args, **kwargs)


resolve_inline_position = _resolve_inline_position

if TYPE_CHECKING:
    from teatree.cli.review import ReviewService


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
    note_type = first_note.get("type") if isinstance(first_note, dict) else None
    if note_type != "DiffNote":
        return f"Comment posted but not anchored inline (type={note_type!r}). discussion_id={discussion_id}", 1
    record_note_claim(service._resolve_base_url, repo, mr, discussion_id, endpoint="discussions", file=file, line=line)
    notify_review_after_receipt(
        service._resolve_base_url,
        repo,
        mr,
        review_action=ReviewAfterReceipt(
            action="post_comment",
            summary=f"posted inline comment discussion_id={discussion_id} on {repo}!{mr}",
            note_web_url=str(first_note.get("web_url", "")) if isinstance(first_note, dict) else "",
        ),
    )
    return f"OK discussion_id={discussion_id} (inline DiffNote)", 0
