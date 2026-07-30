"""The on-behalf pre-gate is enforced on ``approve`` and ``unapprove`` too (#1013).

``ReviewService.approve``/``unapprove`` are outward, state-changing posts
made under the user's identity, so they must route through the same
satisfiable recorded-approval gate every other on-behalf publishing
method uses (``post_comment``, ``post_draft_note``, ``publish_draft_notes``,
``reply_to_discussion``, ``resolve_discussion``, ``update_note``).

Pre-fix, the CLI consulted only the global ``ask_before_post_on_behalf``
flag — a recorded :class:`OnBehalfApproval` row never satisfied
``approve``/``unapprove``, so the recorded-approval channel was unusable
for them. This module pins the corrected contract: gate ON + no approval
→ refuse without an API call; gate ON + recorded row → publish and
consume the row; gate OFF → publish.
"""

from pathlib import Path
from typing import Any

import pytest

from teatree.cli.review import ReviewService
from teatree.core.models import ConfigSetting, OnBehalfApproval

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    # ``on_behalf_post_mode`` is DB-home (#1775) and drives the gate. Gate OFF =
    # ``immediate`` (the store row), gate ON = ``draft_or_ask`` (the dataclass
    # default — clear any row).
    if on:
        ConfigSetting.objects.clear("on_behalf_post_mode")
    else:
        ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")


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
    service = ReviewService(token="t")
    stub = _ApproveStubAPI()
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service, stub


class TestReviewServiceApproveGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_approve_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.approve("org/repo", 7)

        assert code == 1
        assert "approve-on-behalf" in msg
        # No GitLab call may have happened — the gate short-circuits.
        assert not any(c[0] == "post_status" for c in stub.calls)

    def test_approve_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="approve", approver_id="souliane")
        service, stub = _service_with_stub()

        msg, code = service.approve("org/repo", 7)

        assert code == 0, msg
        assert "OK approved" in msg
        assert any(c[0] == "post_status" and c[1].endswith("/approve") for c in stub.calls)

    def test_approve_proceeds_when_gate_off(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=False)
        service, stub = _service_with_stub()

        _, code = service.approve("org/repo", 7)
        assert code == 0
        assert any(c[0] == "post_status" and c[1].endswith("/approve") for c in stub.calls)

    def test_approve_consumes_the_recorded_approval_single_use(self) -> None:
        """A recorded approval is single-use: the second call must refuse."""
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="approve", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code1 = service.approve("org/repo", 7)
        assert code1 == 0
        _, code2 = service.approve("org/repo", 7)
        assert code2 == 1


class TestReviewServiceUnapproveGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_unapprove_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.unapprove("org/repo", 7)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert not any(c[0] == "post_status" for c in stub.calls)

    def test_unapprove_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="unapprove", approver_id="souliane")
        service, stub = _service_with_stub()

        msg, code = service.unapprove("org/repo", 7)

        assert code == 0, msg
        assert "OK unapproved" in msg
        assert any(c[0] == "post_status" and c[1].endswith("/unapprove") for c in stub.calls)

    def test_unapprove_proceeds_when_gate_off(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=False)
        service, stub = _service_with_stub()

        _, code = service.unapprove("org/repo", 7)
        assert code == 0
        assert any(c[0] == "post_status" and c[1].endswith("/unapprove") for c in stub.calls)
