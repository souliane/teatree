"""Identity-bound approval helpers for the review CLI.

Extracted from :mod:`teatree.cli.review` to keep that file under the
module-health LOC cap once the outbound_audit hooks (#1019) landed.
The approval flow is a distinct concern from notes/discussions: it
encodes the review-before-approve doctrine (an approval may only be
recorded once the same identity has left a reviewing footprint), so
it sits naturally in its own file.
"""

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabAPI
    from teatree.types import RawAPIDict


def identity_has_reviewed(api: "GitLabAPI", encoded_repo: str, mr: int) -> tuple[bool, str]:
    """Whether the approving identity left a reviewing footprint on this MR.

    Encodes the review-before-approve doctrine — an approval may only be
    recorded once the same identity has reviewed — WITHOUT forcing a
    content-free public "APPROVE" prose note (souliane/teatree#2716). The
    doctrine's purpose is an anti-rubber-stamp guarantee, not a public
    comment, so any of three footprints satisfies it, none of which is a
    colleague-visible auto-comment:

    * a published note authored by the approver (a genuine inline finding
        — still fine, never auto-posted just to clear this gate);
    * a **draft** note authored by the approver (a colleague-invisible
        review left as a draft — no public comment);
    * the recorded internal verdict — an :class:`OnBehalfApproval` for
        ``(<repo>!<mr>, "approve")``, the human-recorded, maker!=checker
        attribution the on-behalf approve path already requires.

    Returns ``(reviewed, error)``; ``error`` is non-empty only when the
    identity itself cannot be resolved (a hard precondition failure, not
    "no review yet").
    """
    username = api.current_username()
    if not username:
        return False, "Could not resolve the approving GitLab identity (check token / `glab auth status`)."
    if _identity_authored_published_note(api, encoded_repo, mr, username):
        return True, ""
    if _identity_authored_draft_note(api, encoded_repo, mr, username):
        return True, ""
    if _internal_approve_verdict_recorded(encoded_repo, mr):
        return True, ""
    return False, ""


def _identity_authored_published_note(api: "GitLabAPI", encoded_repo: str, mr: int, username: str) -> bool:
    """True iff *username* authored any published note in any discussion thread."""
    discussions = api.get_json_paginated(f"projects/{encoded_repo}/merge_requests/{mr}/discussions?per_page=100")
    for discussion in discussions:
        if not isinstance(discussion, dict):
            continue
        notes = discussion.get("notes")
        if not isinstance(notes, list):
            continue
        for note in notes:
            if _note_authored_by(note, username):
                return True
    return False


def _identity_authored_draft_note(api: "GitLabAPI", encoded_repo: str, mr: int, username: str) -> bool:
    """True iff *username* authored a colleague-invisible draft note on this MR.

    A draft is a genuine review that has not been published — it satisfies
    the review-before-approve doctrine without any public comment.
    """
    drafts = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}/draft_notes")
    if not isinstance(drafts, list):
        return False
    return any(_note_authored_by(draft, username) for draft in drafts)


def _internal_approve_verdict_recorded(encoded_repo: str, mr: int) -> bool:
    """True iff a human-recorded internal approve verdict exists for this MR.

    The recorded :class:`OnBehalfApproval` for ``(<repo>!<mr>, "approve")``
    is the internal attribution the on-behalf approve path requires — a
    durable, maker!=checker record that a human reviewed and authorised the
    approval. Its presence is a reviewing footprint that needs no public
    note. Read lazily so the CLI imports before ``django.setup()``.
    """
    from teatree.core.models.on_behalf_approval import OnBehalfApproval  # noqa: PLC0415

    repo = encoded_repo.replace("%2F", "/")
    return OnBehalfApproval.has_unconsumed(f"{repo}!{mr}", "approve")


def _note_authored_by(note: object, username: str) -> bool:
    """True iff *note* is a dict whose ``author.username`` equals *username*."""
    if not isinstance(note, dict):
        return False
    author = cast("RawAPIDict", note).get("author")
    return isinstance(author, dict) and cast("RawAPIDict", author).get("username") == username


def identity_in_approved_by(api: "GitLabAPI", encoded_repo: str, mr: int) -> bool:
    """Whether the approving identity is already in the MR's ``approved_by``.

    GitLab's ``POST /merge_requests/:iid/approve`` returns ``401
    Unauthorized`` for *both* a genuine auth failure and the idempotent
    case where this identity has already approved (#1029). The two are
    distinguished only by probing ``GET /merge_requests/:iid/approvals``:
    a username match in ``approved_by[*].user.username`` means the
    approve is a no-op success; no match (or an unresolvable identity)
    means the failure is real and must surface.
    """
    username = api.current_username()
    if not username:
        return False
    approvals = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}/approvals")
    if not isinstance(approvals, dict):
        return False
    approved_by = approvals.get("approved_by")
    if not isinstance(approved_by, list):
        return False
    for entry in approved_by:
        if not isinstance(entry, dict):
            continue
        user = cast("RawAPIDict", entry).get("user")
        if isinstance(user, dict) and cast("RawAPIDict", user).get("username") == username:
            return True
    return False


__all__ = ["identity_has_reviewed", "identity_in_approved_by"]
