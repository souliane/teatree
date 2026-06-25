"""Approving never forces a public prose APPROVE note (souliane/teatree#2716).

The recurring user complaint: ``t3 review approve`` refused unless a note
authored by the approver's identity already existed on the MR (the
review-before-approve doctrine, ``identity_has_reviewed``). To satisfy it
the agent had to post a content-free "APPROVE" prose comment from the
approver's identity first — the useless approval-summary comment the user
repeatedly deletes (concrete instance: a now-deleted note on a real MR).

The corrected contract: an *internal* reviewing footprint satisfies the
precondition so no public comment is ever forced. The two internal
footprints, neither of which is a colleague-visible post:

* a **draft note** authored by the approver (colleague-invisible review);
* the recorded :class:`OnBehalfApproval` for ``(<repo>!<mr>, "approve")`` —
    the human-recorded, maker!=checker internal verdict/attribution that the
    on-behalf approve path already requires.

Approval then records the GitLab ``approved_by`` (verify-after-post) and
the internal audit, and posts ZERO notes/discussions. The maker!=checker
and on-behalf gates are untouched — only the *public prose note* is gone.
"""

from pathlib import Path
from typing import Any

import pytest

from teatree.cli.review import ReviewService
from teatree.core.models import ConfigSetting, OnBehalfApproval

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class _SilentApproveStubAPI:
    """``GitLabAPI`` stand-in with NO published note authored by the approver.

    ``discussions`` and ``draft_notes`` come back empty — there is no public
    prose footprint at all. The only attribution is the recorded
    :class:`OnBehalfApproval`. Records every call so the test can assert
    nothing was posted to the MR.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def current_username(self) -> str:
        self.calls.append(("current_username", "", None))
        return "souliane"

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        if endpoint.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": "souliane"}}]}
        if endpoint.endswith("/draft_notes"):
            return []
        return []

    def get_json_paginated(self, endpoint: str) -> list:
        self.calls.append(("get_json_paginated", endpoint, None))
        # No published discussion notes by anyone — no public footprint.
        return []

    def post_json(self, endpoint: str, payload: object) -> object:
        # A posting attempt is a contract violation under this test.
        self.calls.append(("post_json", endpoint, payload))
        return {"id": 1}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        return 200


def _immediate_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the on-behalf gate (mode=immediate) to isolate the review-first precondition."""
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")


def _service_with_stub() -> tuple[ReviewService, _SilentApproveStubAPI]:
    service = ReviewService(token="t")
    stub = _SilentApproveStubAPI()
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service, stub


def _posted_to_mr(stub: _SilentApproveStubAPI) -> list[tuple[str, str, Any]]:
    """Every call that would create a public MR note or discussion."""
    return [
        c
        for c in stub.calls
        if c[0] == "post_json" and ("/notes" in c[1] or "/discussions" in c[1] or "/draft_notes" in c[1])
    ]


class TestApproveOnBehalfNeedsNoPublicNote:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_recorded_approval_satisfies_precondition_with_no_public_note(self) -> None:
        """On-behalf approve succeeds via the internal verdict, posting ZERO notes."""
        _immediate_gate(self.tmp_path, self.monkeypatch)
        # The internal verdict/attribution record — human-recorded, maker!=checker.
        OnBehalfApproval.record(target="org/repo!7", action="approve", approver_id="souliane")
        service, stub = _service_with_stub()

        msg, code = service.approve("org/repo", 7)

        assert code == 0, msg
        assert "OK approved" in msg
        # The GitLab approval landed...
        assert any(c[0] == "post_status" and c[1].endswith("/approve") for c in stub.calls)
        # ...and NOT ONE note/discussion/draft was posted to the MR.
        assert _posted_to_mr(stub) == []

    def test_no_footprint_and_no_recorded_approval_still_refuses(self) -> None:
        """Without ANY footprint (no note, no draft, no recorded approval) approve refuses.

        Maker!=checker / anti-rubber-stamp is preserved: with neither a
        reviewing footprint nor the internal verdict record, approve must
        still refuse — and still post nothing.
        """
        _immediate_gate(self.tmp_path, self.monkeypatch)
        service, stub = _service_with_stub()

        msg, code = service.approve("org/repo", 7)

        assert code == 1
        assert "review before approve" in msg
        assert not any(c[0] == "post_status" for c in stub.calls)
        assert _posted_to_mr(stub) == []


class _DraftReviewStubAPI(_SilentApproveStubAPI):
    """Like the silent stub, but the approver left a DRAFT note (no public note)."""

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        if endpoint.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": "souliane"}}]}
        if endpoint.endswith("/draft_notes"):
            return [{"author": {"username": "souliane"}, "note": "LGTM modulo the edge case"}]
        return []


class TestApproveAcceptsDraftFootprint:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_draft_note_footprint_satisfies_precondition(self) -> None:
        """A colleague-invisible draft review satisfies the precondition — no public note forced."""
        _immediate_gate(self.tmp_path, self.monkeypatch)
        service = ReviewService(token="t")
        stub = _DraftReviewStubAPI()
        service._get_api = lambda: stub  # type: ignore[method-assign]

        msg, code = service.approve("org/repo", 7)

        assert code == 0, msg
        assert "OK approved" in msg
        assert _posted_to_mr(stub) == []
