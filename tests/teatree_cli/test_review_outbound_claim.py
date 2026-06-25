"""``ReviewService`` records an :class:`OutboundClaim` on every successful publish (#1019).

Each gated colleague-facing method (post comment, post draft note,
publish drafts, reply, resolve, update, approve, unapprove) appends one
row to the claim ledger so the drift verifier can later confirm the
artifact actually exists in GitLab.
"""

from pathlib import Path
from typing import Any

import pytest

from teatree.cli.review import ReviewService
from teatree.core.models import ConfigSetting, OnBehalfApproval, OutboundClaim

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _gate_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``on_behalf_post_mode`` is DB-home (#1775): IMMEDIATE turns the gate off.
    # A TOML key would be ignored on read.
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")


class _StubAPI:
    """In-memory stand-in for :class:`GitLabAPI` — every call returns a happy stub."""

    def __init__(self, *, approvers: list[str] | None = None) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.approvers = ["souliane"] if approvers is None else approvers

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        return {"id": 42, "notes": [{"type": "DiffNote", "id": 42}], "line_code": "abc123"}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        return 200

    def put_status(self, endpoint: str, payload: object | None = None) -> int:
        self.calls.append(("put_status", endpoint, payload))
        return 200

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        # Verify-after-post (#2081) reads the artifact back: confirm it landed
        # so the happy path stays green. A bare note/draft-note by id → present;
        # /approvals → this identity approved; the bulk-publish lists →
        # draft_notes flushed to empty, at least one authored note present; a
        # discussion → its resolvable notes carry the requested flag.
        last = endpoint.rstrip("/").rsplit("/", 1)[-1]
        if last.isdigit():
            return {"id": int(last), "resolvable": True, "resolved": True}
        if endpoint.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": u}} for u in self.approvers]}
        if last == "draft_notes":
            return []  # all drafts published
        if last == "notes":
            return [{"id": 99, "author": {"username": "souliane"}}]
        if "discussions/" in endpoint:
            return {"notes": [{"resolvable": True, "resolved": True}]}
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


def _service(*, approvers: list[str] | None = None) -> tuple[ReviewService, _StubAPI]:
    s = ReviewService(token="t")
    stub = _StubAPI(approvers=approvers)
    s._get_api = lambda: stub  # type: ignore[method-assign]
    s._resolve_base_url = lambda: "https://gitlab.example.com/api/v4"  # type: ignore[method-assign]
    return s, stub


class TestReviewServiceRecordsOutboundClaims:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_off(tmp_path, monkeypatch)

    def test_post_comment_general_records_gitlab_note_claim(self) -> None:
        service, _ = _service()
        msg, code = service.post_comment("org/repo", 7, "lgtm")
        assert code == 0, msg
        claim = OutboundClaim.objects.get(idempotency_key="gitlab_note:org/repo!7:42")
        assert claim.kind == OutboundClaim.Kind.GITLAB_NOTE
        assert claim.target_url == "https://gitlab.example.com/org/repo/-/merge_requests/7"
        assert claim.extra["repo"] == "org/repo"
        assert claim.extra["mr"] == 7

    def test_post_draft_note_general_records_claim(self) -> None:
        service, _ = _service()
        msg, code = service.post_draft_note("org/repo", 9, "stage")
        assert code == 0, msg
        assert OutboundClaim.objects.filter(
            idempotency_key="gitlab_note:org/repo!9:42",
        ).exists()

    def test_publish_draft_notes_records_bulk_publish_claim(self) -> None:
        service, _ = _service()
        msg, code = service.publish_draft_notes("org/repo", 11)
        assert code == 0, msg
        assert OutboundClaim.objects.filter(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            idempotency_key="gitlab_note:org/repo!11:bulk_publish",
        ).exists()

    def test_reply_to_discussion_records_claim(self) -> None:
        service, _ = _service()
        msg, code = service.reply_to_discussion("org/repo", 12, "disc-1", "thanks")
        assert code == 0, msg
        assert OutboundClaim.objects.filter(
            idempotency_key="gitlab_note:org/repo!12:42",
        ).exists()

    def test_resolve_discussion_records_claim(self) -> None:
        service, _ = _service()
        msg, code = service.resolve_discussion("org/repo", 13, "disc-2", resolved=True)
        assert code == 0, msg
        assert OutboundClaim.objects.filter(
            idempotency_key="gitlab_note:org/repo!13:disc-2#resolved=true",
        ).exists()

    def test_update_note_draft_records_claim(self) -> None:
        service, _ = _service()
        msg, code = service.update_note("org/repo", 14, 500, "fixed")
        assert code == 0, msg
        assert OutboundClaim.objects.filter(
            idempotency_key="gitlab_note:org/repo!14:update:draft:500",
        ).exists()

    def test_approve_records_gitlab_approve_claim(self) -> None:
        service, _ = _service()
        msg, code = service.approve("org/repo", 15)
        assert code == 0, msg
        claim = OutboundClaim.objects.get(idempotency_key="gitlab_approve:org/repo!15:approve")
        assert claim.kind == OutboundClaim.Kind.GITLAB_APPROVE
        assert claim.extra["endpoint"] == "approve"

    def test_unapprove_records_gitlab_approve_claim_with_unapprove_endpoint(self) -> None:
        # After unapprove the read-back must show this identity ABSENT from
        # approved_by (verify_unapproval_landed, #2081).
        service, _ = _service(approvers=[])
        msg, code = service.unapprove("org/repo", 16)
        assert code == 0, msg
        claim = OutboundClaim.objects.get(idempotency_key="gitlab_approve:org/repo!16:unapprove")
        assert claim.kind == OutboundClaim.Kind.GITLAB_APPROVE
        assert claim.extra["endpoint"] == "unapprove"


class TestFailurePathsDoNotRecordClaims:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_off(tmp_path, monkeypatch)

    def test_approve_failure_does_not_record(self) -> None:
        # A genuine 500 with the identity NOT in approved_by — the idempotent
        # "already approved" probe (#1029) must not mask the real failure.
        service, stub = _service(approvers=[])
        stub.post_status = lambda _endpoint: 500  # type: ignore[method-assign]
        _msg, code = service.approve("org/repo", 7)
        assert code == 1
        assert not OutboundClaim.objects.filter(kind=OutboundClaim.Kind.GITLAB_APPROVE).exists()

    def test_review_first_precondition_blocks_approve_and_records_no_claim(self) -> None:
        service, stub = _service()
        stub.get_json_paginated = lambda _endpoint: []  # type: ignore[method-assign]
        _msg, code = service.approve("org/repo", 8)
        assert code == 1
        assert not OutboundClaim.objects.filter(kind=OutboundClaim.Kind.GITLAB_APPROVE).exists()


class TestApprovedRecordedReviewRecordsAClaim:
    """The publish-success path writes the claim even with the gate ON.

    Recording an :class:`OnBehalfApproval` lets the post proceed; the
    successful publish must still surface in the claim ledger so the
    drift verifier can audit it.
    """

    def test_post_comment_with_recorded_approval_records_claim(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text("[teatree]\nask_before_post_on_behalf = true\n", encoding="utf-8")
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
        # After #1207 the default-draft path is gated on ``post_draft_note``
        # (not ``post_comment``) — that's the action the recorded approval
        # must name to satisfy the gate on the live, default-draft branch.
        OnBehalfApproval.record(target="org/repo!19", action="post_draft_note", approver_id="souliane")

        service, _ = _service()
        msg, code = service.post_comment("org/repo", 19, "thanks")
        assert code == 0, msg
        assert OutboundClaim.objects.filter(
            idempotency_key="gitlab_note:org/repo!19:42",
        ).exists()
