"""Confirm a GitLab ``draft_notes/bulk_publish`` landed, against what the MR held before it (#2081).

Any note on the MR is not evidence — every real MR carries system notes ("added 1
commit") — so a publish is confirmed only by its drafts flushing AND a new non-system
note authored by the posting identity.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.cli.review.audit import ReviewArtifactNotVerifiedError

if TYPE_CHECKING:
    from teatree.backends.gitlab.api import GitLabAPI


@dataclass(frozen=True, slots=True)
class BulkPublishBaseline:
    """What an MR held before a bulk publish: the drafts it will flush and the notes already authored."""

    pending_drafts: int
    authored_note_ids: frozenset[int]

    @classmethod
    def read(cls, api: "GitLabAPI", encoded: str, mr: int) -> "BulkPublishBaseline":
        return cls(
            pending_drafts=_pending_draft_count(api, encoded, mr),
            authored_note_ids=_authored_note_ids(api, encoded, mr),
        )

    def verify_published(self, api: "GitLabAPI", encoded: str, mr: int) -> None:
        """Raise unless the drafts flushed and a new note by the posting identity landed.

        Transport errors propagate unchanged (transient, not a failed post).
        """
        if remaining := _pending_draft_count(api, encoded, mr):
            msg = f"bulk publish reported OK but {remaining} draft note(s) remain on !{mr} — not reporting as published"
            raise ReviewArtifactNotVerifiedError(msg)
        if not _authored_note_ids(api, encoded, mr) - self.authored_note_ids:
            msg = (
                f"bulk publish reported OK but no new note by the posting identity landed on !{mr} "
                f"({self.pending_drafts} draft(s) were pending) — not reporting as published"
            )
            raise ReviewArtifactNotVerifiedError(msg)


def _pending_draft_count(api: "GitLabAPI", encoded: str, mr: int) -> int:
    return len(api.get_json_paginated(f"projects/{encoded}/merge_requests/{mr}/draft_notes?per_page=100"))


def _authored_note_ids(api: "GitLabAPI", encoded: str, mr: int) -> frozenset[int]:
    """Ids of the MR's non-system notes authored by the posting identity; an unresolvable identity is not verified."""
    username = api.current_username()
    if not username:
        msg = f"the posting identity could not be resolved, so a bulk publish on !{mr} cannot be confirmed"
        raise ReviewArtifactNotVerifiedError(msg)
    notes = api.get_json_paginated(f"projects/{encoded}/merge_requests/{mr}/notes?per_page=100")
    return frozenset(
        note_id
        for note in notes
        if isinstance(note, dict)
        and not note.get("system")
        and isinstance(author := note.get("author"), dict)
        and author.get("username") == username
        and isinstance(note_id := note.get("id"), int)
    )


__all__ = ["BulkPublishBaseline"]
