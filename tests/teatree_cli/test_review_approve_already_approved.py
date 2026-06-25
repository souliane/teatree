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
from teatree.core.models import ConfigSetting

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _gate_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``on_behalf_post_mode`` is DB-home (#1775): IMMEDIATE turns the gate off.
    # A TOML key would be ignored on read.
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    ConfigSetting.objects.set_value("on_behalf_post_mode", "immediate")


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
    service = ReviewService(token="t")
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service


class TestApproveAlreadyApprovedIsIdempotent:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_off(tmp_path, monkeypatch)

    def test_already_approved_401_reports_success(self) -> None:
        stub = _AlreadyApprovedAPI()
        service = _service_with(stub)

        msg, code = service.approve("org/repo", 7)

        assert code == 0, msg
        assert "Already approved by souliane" in msg
        assert any(c[0] == "get_json" and c[1].endswith("/approvals") for c in stub.calls)

    def test_genuine_401_still_fails(self) -> None:
        stub = _GenuineAuthFailureAPI()
        service = _service_with(stub)

        msg, code = service.approve("org/repo", 7)

        assert code == 1
        assert "Failed" in msg
        assert "401" in msg
