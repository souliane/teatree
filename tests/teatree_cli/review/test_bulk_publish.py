"""A bulk publish is confirmed against what the MR held before it, never by any note on the MR (#2081)."""

import pytest

from teatree.cli.review.audit import ReviewArtifactNotVerifiedError
from teatree.cli.review.bulk_publish import BulkPublishBaseline


class _API:
    """GitLab list reads keyed by endpoint suffix, and a posting identity."""

    def __init__(self, results: dict[str, list[object]]) -> None:
        self.results = results
        self.username = "souliane"

    def get_json_paginated(self, endpoint: str) -> list[object]:
        path = endpoint.split("?", 1)[0]
        return next((value for suffix, value in self.results.items() if path.endswith(suffix)), [])

    def current_username(self) -> str:
        return self.username


_ONE_PENDING = BulkPublishBaseline(pending_drafts=1, authored_note_ids=frozenset({5}))


def _note(note_id: int, *, author: str = "souliane", system: bool = False) -> dict[str, object]:
    return {"id": note_id, "system": system, "author": {"username": author}}


class TestVerifyPublished:
    def test_drafts_flushed_and_a_new_authored_note_passes(self) -> None:
        api = _API(results={"/draft_notes": [], "/notes": [_note(5), _note(6)]})
        _ONE_PENDING.verify_published(api, "org%2Frepo", 7)

    def test_drafts_still_present_raises(self) -> None:
        api = _API(results={"/draft_notes": [{"id": 1}], "/notes": [_note(5), _note(6)]})
        with pytest.raises(ReviewArtifactNotVerifiedError, match="remain"):
            _ONE_PENDING.verify_published(api, "org%2Frepo", 7)

    def test_only_the_notes_already_there_raises(self) -> None:
        api = _API(results={"/draft_notes": [], "/notes": [_note(5)]})
        with pytest.raises(ReviewArtifactNotVerifiedError, match="no new note"):
            _ONE_PENDING.verify_published(api, "org%2Frepo", 7)

    def test_a_new_note_by_someone_else_raises(self) -> None:
        api = _API(results={"/draft_notes": [], "/notes": [_note(5), _note(6, author="colleague")]})
        with pytest.raises(ReviewArtifactNotVerifiedError, match="no new note"):
            _ONE_PENDING.verify_published(api, "org%2Frepo", 7)

    def test_a_new_system_note_raises(self) -> None:
        api = _API(results={"/draft_notes": [], "/notes": [_note(5), _note(6, system=True)]})
        with pytest.raises(ReviewArtifactNotVerifiedError, match="no new note"):
            _ONE_PENDING.verify_published(api, "org%2Frepo", 7)

    def test_an_unresolvable_identity_is_not_verified(self) -> None:
        api = _API(results={"/draft_notes": [], "/notes": [_note(5), _note(6)]})
        api.username = ""
        with pytest.raises(ReviewArtifactNotVerifiedError, match="identity"):
            _ONE_PENDING.verify_published(api, "org%2Frepo", 7)


class TestRead:
    def test_counts_every_pending_draft_and_only_this_identitys_notes(self) -> None:
        api = _API(
            results={
                "/draft_notes": [{"id": 1}, {"id": 2}],
                "/notes": [_note(5), _note(6, author="colleague"), _note(7, system=True)],
            }
        )
        assert BulkPublishBaseline.read(api, "org%2Frepo", 7) == BulkPublishBaseline(
            pending_drafts=2, authored_note_ids=frozenset({5})
        )
