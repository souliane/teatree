"""Unit coverage for the verify-after-post helpers in ``teatree.cli.review.audit`` (#2081).

These exercise every branch of the read-back predicates in isolation: the
non-id no-op, the 404-is-drift path, the non-404-re-raise (transient) path, the
empty-payload path, and the inverse delete/unapprove/resolve confirmations.
"""

from http import HTTPStatus

import httpx
import pytest

from teatree.cli.review.audit import (
    ReviewArtifactNotVerifiedError,
    verify_approval_landed,
    verify_bulk_publish,
    verify_discussion_resolved,
    verify_note_deleted,
    verify_note_landed,
    verify_unapproval_landed,
)


def _err(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gitlab.example/api/v4/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


class _API:
    """Minimal GitLab API stub: get_json returns a queued value or raises a queued error."""

    def __init__(self, results: dict[str, object] | None = None, errors: dict[str, Exception] | None = None) -> None:
        self.results = results or {}
        self.errors = errors or {}
        self.seen: list[str] = []

    def get_json(self, endpoint: str) -> object:
        self.seen.append(endpoint)
        for needle, exc in self.errors.items():
            if needle in endpoint:
                raise exc
        for needle, value in self.results.items():
            if needle in endpoint:
                return value
        return None

    def current_username(self) -> str:
        return "souliane"


class TestVerifyNoteLanded:
    def test_non_digit_id_is_noop(self) -> None:
        api = _API()
        verify_note_landed(api, "org%2Frepo", 7, "not-a-number", endpoint="notes")
        assert api.seen == []  # nothing to read back

    def test_present_note_passes(self) -> None:
        api = _API(results={"/notes/42": {"id": 42}})
        verify_note_landed(api, "org%2Frepo", 7, 42, endpoint="notes")

    def test_draft_note_endpoint_reads_draft_notes(self) -> None:
        api = _API(results={"/draft_notes/42": {"id": 42}})
        verify_note_landed(api, "org%2Frepo", 7, 42, endpoint="draft_notes")
        assert any("draft_notes/42" in e for e in api.seen)

    def test_404_raises_not_verified(self) -> None:
        api = _API(errors={"/notes/42": _err(HTTPStatus.NOT_FOUND)})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_note_landed(api, "org%2Frepo", 7, 42, endpoint="notes")

    def test_transient_error_reraises(self) -> None:
        api = _API(errors={"/notes/42": _err(HTTPStatus.SERVICE_UNAVAILABLE)})
        with pytest.raises(httpx.HTTPStatusError):
            verify_note_landed(api, "org%2Frepo", 7, 42, endpoint="notes")

    def test_none_payload_raises_not_verified(self) -> None:
        api = _API(results={})  # get_json returns None
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_note_landed(api, "org%2Frepo", 7, 42, endpoint="notes")


class TestVerifyNoteDeleted:
    def test_non_digit_id_is_noop(self) -> None:
        api = _API()
        verify_note_deleted(api, "org%2Frepo", 7, "x")
        assert api.seen == []

    def test_404_confirms_deletion(self) -> None:
        api = _API(errors={"/notes/9": _err(HTTPStatus.NOT_FOUND)})
        verify_note_deleted(api, "org%2Frepo", 7, 9)

    def test_still_present_raises(self) -> None:
        api = _API(results={"/notes/9": {"id": 9}})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_note_deleted(api, "org%2Frepo", 7, 9)

    def test_transient_error_reraises(self) -> None:
        api = _API(errors={"/notes/9": _err(HTTPStatus.SERVICE_UNAVAILABLE)})
        with pytest.raises(httpx.HTTPStatusError):
            verify_note_deleted(api, "org%2Frepo", 7, 9)


class TestVerifyBulkPublish:
    def test_drafts_flushed_and_authored_note_present_passes(self) -> None:
        api = _API(results={"/draft_notes": [], "/notes": [{"id": 1}]})
        verify_bulk_publish(api, "org%2Frepo", 7)

    def test_drafts_still_present_raises(self) -> None:
        api = _API(results={"/draft_notes": [{"id": 1}]})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_bulk_publish(api, "org%2Frepo", 7)

    def test_no_authored_notes_raises(self) -> None:
        api = _API(results={"/draft_notes": [], "/notes": []})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_bulk_publish(api, "org%2Frepo", 7)


class TestVerifyApprovalLanded:
    def test_present_passes(self) -> None:
        api = _API(results={"/approvals": {"approved_by": [{"user": {"username": "souliane"}}]}})
        verify_approval_landed(api, "org%2Frepo", 7)

    def test_absent_raises(self) -> None:
        api = _API(results={"/approvals": {"approved_by": []}})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_approval_landed(api, "org%2Frepo", 7)


class TestVerifyUnapprovalLanded:
    def test_absent_passes(self) -> None:
        api = _API(results={"/approvals": {"approved_by": []}})
        verify_unapproval_landed(api, "org%2Frepo", 7)

    def test_still_present_raises(self) -> None:
        api = _API(results={"/approvals": {"approved_by": [{"user": {"username": "souliane"}}]}})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_unapproval_landed(api, "org%2Frepo", 7)


class TestVerifyDiscussionResolved:
    def test_matching_flag_passes(self) -> None:
        api = _API(results={"/discussions/d1": {"notes": [{"resolvable": True, "resolved": True}]}})
        verify_discussion_resolved(api, "org%2Frepo", 7, "d1", resolved=True)

    def test_mismatched_flag_raises(self) -> None:
        api = _API(results={"/discussions/d1": {"notes": [{"resolvable": True, "resolved": False}]}})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_discussion_resolved(api, "org%2Frepo", 7, "d1", resolved=True)

    def test_no_resolvable_notes_raises(self) -> None:
        api = _API(results={"/discussions/d1": {"notes": [{"resolvable": False}]}})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_discussion_resolved(api, "org%2Frepo", 7, "d1", resolved=True)

    def test_404_raises_not_verified(self) -> None:
        api = _API(errors={"/discussions/d1": _err(HTTPStatus.NOT_FOUND)})
        with pytest.raises(ReviewArtifactNotVerifiedError):
            verify_discussion_resolved(api, "org%2Frepo", 7, "d1", resolved=True)

    def test_transient_error_reraises(self) -> None:
        api = _API(errors={"/discussions/d1": _err(HTTPStatus.SERVICE_UNAVAILABLE)})
        with pytest.raises(httpx.HTTPStatusError):
            verify_discussion_resolved(api, "org%2Frepo", 7, "d1", resolved=True)
