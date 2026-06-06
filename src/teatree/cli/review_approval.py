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
    """Whether the approving identity already authored a note on this MR.

    Encodes the review-before-approve doctrine: an approval may only be
    recorded once the same identity has left a reviewing footprint
    (any note in any discussion thread). Returns ``(reviewed, error)``;
    ``error`` is non-empty only when the identity itself cannot be
    resolved (a hard precondition failure, not "no review yet").
    """
    username = api.current_username()
    if not username:
        return False, "Could not resolve the approving GitLab identity (check token / `glab auth status`)."
    discussions = api.get_json_paginated(f"projects/{encoded_repo}/merge_requests/{mr}/discussions?per_page=100")
    for discussion in discussions:
        if not isinstance(discussion, dict):
            continue
        notes = discussion.get("notes")
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            author = cast("RawAPIDict", note).get("author")
            if isinstance(author, dict) and cast("RawAPIDict", author).get("username") == username:
                return True, ""
    return False, ""


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
