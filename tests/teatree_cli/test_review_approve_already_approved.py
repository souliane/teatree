"""``approve`` treats GitLab's idempotent already-approved 401 as success (#1029).

GitLab's ``POST /merge_requests/:iid/approve`` returns ``401 Unauthorized``
with body ``{"message":"401 Unauthorized"}`` for *both* a genuine
auth failure (expired/revoked PAT) **and** the idempotent case where the
current identity is already in the MR's ``approved_by`` list. Pre-fix,
``ReviewService.approve`` treated every non-2xx as a hard failure, so a
no-op re-approve printed ``Failed: HTTP 401`` — indistinguishable from a
real token problem and noisy on loop review sweeps.

This module pins the corrected contract: on a non-2xx approve response,
probe ``GET /merge_requests/:iid/approvals``; if the current username is
in ``approved_by[*].user.username`` the approve is idempotently
successful (exit 0, ``Already approved by <username>``). A genuine 401
(identity NOT in ``approved_by``) must still fail.
"""

from pathlib import Path
from typing import Any

import pytest

from teatree.cli.review import ReviewService
from tests.teatree_core._on_behalf_gate_helpers import OWNED_REPO, seed_permitting_posture

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _gate_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A permitting posture turns the gate off.
    seed_permitting_posture()


class _AlreadyApprovedAPI:
    """GitLab stub: ``approve`` 401s, but the current user IS in ``approved_by``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def current_username(self) -> str:
        return "souliane"

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint))
        if endpoint.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": "souliane"}}]}
        return []

    def get_json_paginated(self, endpoint: str) -> list:
        self.calls.append(("get_json_paginated", endpoint))
        # discussions probe for the review-before-approve precondition
        return [{"notes": [{"author": {"username": "souliane"}}]}]

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint))
        return 401


class _GenuineAuthFailureAPI:
    """GitLab stub: ``approve`` 401s and the current user is NOT in ``approved_by``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def current_username(self) -> str:
        return "souliane"

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint))
        if endpoint.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": "someone-else"}}]}
        return []

    def get_json_paginated(self, endpoint: str) -> list:
        self.calls.append(("get_json_paginated", endpoint))
        return [{"notes": [{"author": {"username": "souliane"}}]}]

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint))
        return 401


def _service_with(stub: Any) -> ReviewService:
    return ReviewService(token="t", api=stub)


class TestApproveAlreadyApprovedIsIdempotent:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_off(tmp_path, monkeypatch)

    def test_already_approved_401_reports_success(self) -> None:
        stub = _AlreadyApprovedAPI()
        service = _service_with(stub)

        msg, code = service.approve(OWNED_REPO, 7)

        assert code == 0, msg
        assert "Already approved by souliane" in msg
        assert any(c[0] == "get_json" and c[1].endswith("/approvals") for c in stub.calls)

    def test_genuine_401_still_fails(self) -> None:
        stub = _GenuineAuthFailureAPI()
        service = _service_with(stub)

        msg, code = service.approve(OWNED_REPO, 7)

        assert code == 1
        assert "Failed" in msg
        assert "401" in msg


class _SelfAuthoredMrAPI:
    """GitLab stub: ``approve`` 401s because the MR was opened under THIS identity."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def current_username(self) -> str:
        return "souliane"

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint))
        if endpoint.endswith("/approvals"):
            return {"approved_by": []}
        return {"author": {"username": "souliane"}}

    def get_json_paginated(self, endpoint: str) -> list:
        self.calls.append(("get_json_paginated", endpoint))
        return [{"notes": [{"author": {"username": "souliane"}}]}]

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint))
        return 401


class _ForbiddenAPI(_SelfAuthoredMrAPI):
    """Same self-authored MR, but the forge answered a non-401 status."""

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint))
        return 403


class TestApprove401NamesSelfApproval:
    """A 401 on an MR this identity AUTHORED names the author, not a credential problem.

    A forge bars an MR's author from approving it and reports that as 401 rather than 403, so a
    bare ``Failed: HTTP 401`` is indistinguishable from a dead token — and sends the reader to
    the secret store while the real fault is that the MR was opened under the wrong identity.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_off(tmp_path, monkeypatch)

    def test_a_self_authored_mr_401_names_the_author_and_the_sanctioned_create_paths(self) -> None:
        stub = _SelfAuthoredMrAPI()

        msg, code = _service_with(stub).approve(OWNED_REPO, 7)

        assert code == 1
        assert "401" in msg
        assert "AUTHORED by 'souliane'" in msg
        assert "The credential is not the fault." in msg
        assert "pr create" in msg
        assert "ensure-pr" in msg

    def test_a_colleague_authored_mr_401_keeps_the_bare_status(self) -> None:
        stub = _GenuineAuthFailureAPI()

        msg, _code = _service_with(stub).approve(OWNED_REPO, 7)

        assert "AUTHORED by" not in msg

    def test_a_non_401_refusal_is_never_attributed_to_self_approval(self) -> None:
        # Another status has its own cause; naming self-approval on all of them is a guess.
        stub = _ForbiddenAPI()

        msg, code = _service_with(stub).approve(OWNED_REPO, 7)

        assert code == 1
        assert "403" in msg
        assert "AUTHORED by" not in msg
