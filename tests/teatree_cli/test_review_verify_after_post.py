"""Verify-after-post: a review publish reports success only after a read-back confirms the artifact (#2081).

The incident: a batch of ``post_draft_note`` / ``publish`` events reported
success (with note ids) and ``published all draft notes`` notifications, but a
later drift pass found NONE of the claimed notes present via the GitLab API.
The egress reported success on the dispatch HTTP status with no read-back.

These tests model that exact shape: POST happy (``post_json``/``post_status``
return a success body/code) AND the read-back GET reports the artifact absent
(404 / empty list). The publish must then return a FAILURE (code 1), record NO
``OutboundClaim``, and fire NO after-receipt DM — never the phantom "posted" claim.

A non-404 transport error on the read-back must NOT be reported as a failed
post (that would turn every flaky GET into a phantom-FAILURE — the inverse
incident); it re-raises and the caller surfaces api_unavailable, not a lie.
"""

from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
import pytest

from teatree.cli.review import ReviewService
from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting, OutboundClaim

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _gate_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The on-behalf gate is OFF when the DB-home ``on_behalf_post_mode`` is
    # IMMEDIATE (#1775), resolved from the ``ConfigSetting`` store.
    ConfigSetting.objects.set_value("on_behalf_post_mode", OnBehalfPostMode.IMMEDIATE.value)


def _http_404() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gitlab.example.com/api/v4/x")
    response = httpx.Response(HTTPStatus.NOT_FOUND, request=request)
    return httpx.HTTPStatusError("not found", request=request, response=response)


def _http_503() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gitlab.example.com/api/v4/x")
    response = httpx.Response(HTTPStatus.SERVICE_UNAVAILABLE, request=request)
    return httpx.HTTPStatusError("unavailable", request=request, response=response)


class _PhantomAPI:
    """POST succeeds, but the read-back GET reports the artifact absent (the incident)."""

    def __init__(self, *, read_back_error: Exception | None = None, approvers: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.read_back_error = read_back_error
        self.approvers = approvers if approvers is not None else []

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        return {"id": 42, "notes": [{"type": "DiffNote", "id": 42}], "line_code": "abc123", "web_url": "u"}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        return 200

    def put_status(self, endpoint: str, payload: object | None = None) -> int:
        self.calls.append(("put_status", endpoint, payload))
        return 200

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        # The read-back GET shapes: a note/draft note by id (ends with a
        # digit), the approvals endpoint, or the bulk-publish confirmation
        # lists (.../draft_notes and .../notes). When read_back_error is set,
        # every read-back raises it so the transient-vs-404 discipline is
        # exercised on each path.
        last = endpoint.rstrip("/").rsplit("/", 1)[-1]
        is_artifact_readback = last.isdigit()
        is_approvals = endpoint.endswith("/approvals")
        is_bulk_list = last in {"draft_notes", "notes"}
        if (is_artifact_readback or is_approvals or is_bulk_list) and self.read_back_error is not None:
            raise self.read_back_error
        if is_approvals:
            return {"approved_by": [{"user": {"username": u}} for u in self.approvers]}
        if is_artifact_readback:
            # 404-on-read-back is modelled via read_back_error; an empty/None
            # default here means "not found" for the list-shape read-backs.
            return None
        return []

    def get_json_paginated(self, endpoint: str) -> list:
        self.calls.append(("get_json_paginated", endpoint, None))
        if "discussions" in endpoint:
            return [{"notes": [{"author": {"username": "souliane"}}]}]
        return []

    def current_username(self) -> str:
        return "souliane"

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint, None))
        return 204


def _service(stub: _PhantomAPI) -> ReviewService:
    s = ReviewService(token="t")
    s._get_api = lambda: stub  # type: ignore[method-assign]
    s._resolve_base_url = lambda: "https://gitlab.example.com/api/v4"  # type: ignore[method-assign]
    return s


@pytest.fixture(autouse=True)
def _ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate_off(tmp_path, monkeypatch)


class TestPhantomPostReportsFailure:
    def test_publish_draft_notes_phantom_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The incident's exact signal: bulk_publish returns 200 yet the drafts
        # are STILL present on the MR (nothing actually published).
        class _DraftsStillPresentAPI(_PhantomAPI):
            def get_json(self, endpoint: str) -> object:
                self.calls.append(("get_json", endpoint, None))
                if endpoint.endswith("/draft_notes"):
                    return [{"id": 1, "note": "unpublished"}]  # drafts NOT flushed
                if endpoint.endswith("/notes"):
                    return []  # no authored notes either
                return []

        dms: list[object] = []
        monkeypatch.setattr(
            "teatree.core.on_behalf_post_receipt.notify_user_on_behalf_post",
            lambda **kwargs: dms.append(kwargs),
        )
        stub = _DraftsStillPresentAPI()
        service = _service(stub)
        msg, code = service.publish_draft_notes("org/repo", 11)
        assert code == 1, msg
        assert "all draft notes published" not in msg
        assert not OutboundClaim.objects.filter(idempotency_key="gitlab_note:org/repo!11:bulk_publish").exists()
        assert dms == [], "no after-receipt DM for an unverified publish"

    def test_post_draft_note_phantom_reports_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The default ``post_comment`` path routes through ``post_draft_note`` —
        # the exact path the incident batch used. The draft is the artifact to
        # read back.
        dms: list[object] = []
        monkeypatch.setattr(
            "teatree.core.on_behalf_post_receipt.notify_user_on_behalf_post",
            lambda **kwargs: dms.append(kwargs),
        )
        stub = _PhantomAPI(read_back_error=_http_404())
        service = _service(stub)
        msg, code = service.post_draft_note("org/repo", 7, "lgtm")
        assert code == 1, msg
        assert not msg.startswith("OK draft_note_id=")
        assert not OutboundClaim.objects.filter(idempotency_key="gitlab_note:org/repo!7:42").exists()
        assert dms == [], "no after-receipt DM for an unverified post"

    def test_approve_phantom_reports_failure(self) -> None:
        # approved_by does NOT contain the current user → approve must fail.
        stub = _PhantomAPI(approvers=["someone-else"])
        service = _service(stub)
        msg, code = service.approve("org/repo", 15)
        assert code == 1, msg
        assert not msg.startswith("OK approved")
        assert not OutboundClaim.objects.filter(kind=OutboundClaim.Kind.GITLAB_APPROVE).exists()


class TestTransientReadbackDoesNotReportFailure:
    """A non-404 transport error on the read-back must NOT become a phantom-FAILURE."""

    def test_publish_draft_notes_transient_readback_does_not_claim_failure(self) -> None:
        stub = _PhantomAPI(read_back_error=_http_503())
        service = _service(stub)
        with pytest.raises(httpx.HTTPStatusError):
            service.publish_draft_notes("org/repo", 11)

    def test_approve_transient_readback_propagates(self) -> None:
        stub = _PhantomAPI(read_back_error=_http_503(), approvers=["someone-else"])
        service = _service(stub)
        with pytest.raises(httpx.HTTPStatusError):
            service.approve("org/repo", 15)


class TestVerifiedPostStillSucceeds:
    """The read-back confirms the artifact → success path is preserved."""

    class _ConfirmingAPI(_PhantomAPI):
        def get_json(self, endpoint: str) -> object:
            self.calls.append(("get_json", endpoint, None))
            if endpoint.endswith("/approvals"):
                return {"approved_by": [{"user": {"username": "souliane"}}]}
            if endpoint.rstrip("/").rsplit("/", 1)[-1].isdigit():
                return {"id": 42}
            return []

    def test_post_draft_note_verified_succeeds(self) -> None:
        stub = self._ConfirmingAPI()
        service = _service(stub)
        msg, code = service.post_draft_note("org/repo", 7, "lgtm")
        assert code == 0, msg
        assert OutboundClaim.objects.filter(idempotency_key="gitlab_note:org/repo!7:42").exists()

    def test_publish_draft_notes_verified_succeeds(self) -> None:
        # A bulk publish confirms by listing draft_notes == 0 (all flushed) and
        # at least one authored note present — the incident's exact missed signal.
        class _BulkConfirmAPI(_PhantomAPI):
            def get_json(self, endpoint: str) -> object:
                self.calls.append(("get_json", endpoint, None))
                if endpoint.endswith("/draft_notes"):
                    return []  # zero drafts remaining = all published
                if endpoint.endswith("/notes"):
                    return [{"id": 99, "author": {"username": "souliane"}}]
                return []

        stub = _BulkConfirmAPI()
        service = _service(stub)
        msg, code = service.publish_draft_notes("org/repo", 11)
        assert code == 0, msg
        assert OutboundClaim.objects.filter(idempotency_key="gitlab_note:org/repo!11:bulk_publish").exists()

    def test_approve_verified_succeeds(self) -> None:
        stub = self._ConfirmingAPI(approvers=["souliane"])
        service = _service(stub)
        msg, code = service.approve("org/repo", 15)
        assert code == 0, msg
        assert OutboundClaim.objects.filter(kind=OutboundClaim.Kind.GITLAB_APPROVE).exists()


class TestVerifyFailRollsBackOnBehalfConsume:
    """A verify-FAIL rolls back the on-behalf approval consume + audit (#1879 invariant).

    The verify raises INSIDE the ``publish`` body, so it propagates through the
    same ``transaction.atomic`` as the consume — the approval is NOT burned and
    no ``OnBehalfAudit`` row claims a post that never landed. Exactly like a
    post-failure, so a retry can reuse the same recorded approval.
    """

    def test_phantom_publish_does_not_consume_recorded_approval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from teatree.core.models import OnBehalfApproval, OnBehalfAudit  # noqa: PLC0415

        ConfigSetting.objects.set_value("on_behalf_post_mode", "ask")
        approval = OnBehalfApproval.record(target="org/repo!11", action="publish_draft_notes", approver_id="souliane")

        class _DraftsStillPresentAPI(_PhantomAPI):
            def get_json(self, endpoint: str) -> object:
                self.calls.append(("get_json", endpoint, None))
                if endpoint.endswith("/draft_notes"):
                    return [{"id": 1, "note": "unpublished"}]
                return []

        service = _service(_DraftsStillPresentAPI())
        msg, code = service.publish_draft_notes("org/repo", 11)
        assert code == 1, msg
        approval.refresh_from_db()
        assert approval.consumed_at is None, "approval was consumed despite an unverified publish"
        assert OnBehalfAudit.objects.count() == 0, "audit row written for a publish that did not land"
