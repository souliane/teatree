"""``publish_operator_approval`` is the recorded-consent gate for ``approve`` and ``unapprove`` (#1013).

Both are outward, state-changing posts made under a person's identity, so they publish only
against a recorded :class:`OnBehalfApproval`: no consent → refuse without an API call; recorded
consent → publish and consume it exactly once.
"""

from pathlib import Path
from typing import Any

import pytest

from teatree.cli.review import ReviewService
from teatree.cli.review.operator_approval import publish_operator_approval
from teatree.core.models import OnBehalfApproval
from tests.teatree_core._on_behalf_gate_helpers import OWNED_REPO, seed_forbidding_posture, seed_permitting_posture

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    # The active posture drives the gate, and each state is pinned explicitly:
    # an unstaged posture resolves fail-closed, which would read as gate-ON for
    # reasons the case never chose (#3895).
    seed_forbidding_posture() if on else seed_permitting_posture()


class _ApproveStubAPI:
    """In-memory stand-in for ``GitLabAPI`` — records every network call.

    Returns enough shape for ``_identity_has_reviewed`` to find a prior
    note from ``souliane`` (so the review-before-approve precondition is
    satisfied and the test isolates the on-behalf-gate behaviour).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._unapproved = False

    def current_username(self) -> str:
        self.calls.append(("current_username", "", None))
        return "souliane"

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        # Verify-after-post (#2081): the approve read-back of /approvals must
        # show this identity present (and the unapprove read-back must show it
        # absent) so both confirmed success paths stay green.
        if endpoint.endswith("/approvals"):
            if self._unapproved:
                return {"approved_by": []}
            return {"approved_by": [{"user": {"username": "souliane"}}]}
        return []

    def get_json_paginated(self, endpoint: str) -> list:
        self.calls.append(("get_json_paginated", endpoint, None))
        return [{"notes": [{"author": {"username": "souliane"}}]}]

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        if endpoint.endswith("/unapprove"):
            self._unapproved = True
        return 200


def _service_with_stub() -> tuple[ReviewService, _ApproveStubAPI]:
    stub = _ApproveStubAPI()
    service = ReviewService(token="t", api=stub, oauth=True)
    return service, stub


class TestReviewServiceApproveGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_approve_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target=f"{OWNED_REPO}!7", action="approve", approver_id="gitlab:souliane")
        service, stub = _service_with_stub()

        msg, code = publish_operator_approval(service, OWNED_REPO, 7, "approve", "gitlab:souliane")

        assert code == 0, msg
        assert "OK approved" in msg
        assert any(c[0] == "post_status" and c[1].endswith("/approve") for c in stub.calls)

    def test_approve_requires_consent_when_gate_off(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=False)
        service, stub = _service_with_stub()

        _, code = publish_operator_approval(service, OWNED_REPO, 7, "approve", "gitlab:souliane")
        assert code == 1
        assert not any(c[0] == "post_status" for c in stub.calls)

    def test_approve_consumes_the_recorded_approval_single_use(self) -> None:
        """A recorded approval is single-use: the second call must refuse."""
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target=f"{OWNED_REPO}!7", action="approve", approver_id="gitlab:souliane")
        service, _stub = _service_with_stub()

        _, code1 = publish_operator_approval(service, OWNED_REPO, 7, "approve", "gitlab:souliane")
        assert code1 == 0
        _, code2 = publish_operator_approval(service, OWNED_REPO, 7, "approve", "gitlab:souliane")
        assert code2 == 1


class TestReviewServiceUnapproveGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_unapprove_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target=f"{OWNED_REPO}!7", action="unapprove", approver_id="gitlab:souliane")
        service, stub = _service_with_stub()

        msg, code = publish_operator_approval(service, OWNED_REPO, 7, "unapprove", "gitlab:souliane")

        assert code == 0, msg
        assert "OK unapproved" in msg
        assert any(c[0] == "post_status" and c[1].endswith("/unapprove") for c in stub.calls)

    def test_unapprove_requires_consent_when_gate_off(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=False)
        service, stub = _service_with_stub()

        _, code = publish_operator_approval(service, OWNED_REPO, 7, "unapprove", "gitlab:souliane")
        assert code == 1
        assert not any(c[0] == "post_status" for c in stub.calls)
