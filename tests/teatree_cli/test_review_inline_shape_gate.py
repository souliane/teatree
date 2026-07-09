"""Stay-inline-once-inline review-shape gate wired into the pre-publish chain (PR-08, item 4).

The gate refuses an MR-level (general) draft note when the MR already carries
inline draft notes, so a review that began inline stays inline. These tests
drive the gate through ``ReviewService.post_draft_note`` — the real chain — with
a stub API whose ``draft_notes`` endpoint returns a canned list, so the wiring
(not just the pure function) is exercised. The on-behalf gate is pinned to
IMMEDIATE and the MR author to the current identity so any block is attributable
to this gate.
"""

from pathlib import Path
from typing import Any

import pytest

from teatree.cli.review import ReviewService
from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_AUTHOR_ALICE = "alice"
_INLINE_DRAFT = {"id": 1, "note": "fix this", "position": {"new_path": "a.py", "new_line": 10}}


def _gate_immediate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ConfigSetting.objects.set_value("on_behalf_post_mode", OnBehalfPostMode.IMMEDIATE.value)


class _StubAPI:
    """In-memory ``GitLabAPI`` stand-in; ``draft_notes`` returns *draft_notes*."""

    def __init__(self, draft_notes: list[dict[str, object]]) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._draft_notes = draft_notes

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        return {"id": 9, "notes": [{"type": "DiffNote"}], "line_code": "abc123_10_10"}

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        if endpoint.endswith("/draft_notes"):
            return self._draft_notes
        return {}

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint, None))
        return 204

    def current_username(self) -> str:
        return _AUTHOR_ALICE


def _service_with_stub(draft_notes: list[dict[str, object]]) -> tuple[ReviewService, _StubAPI]:
    service = ReviewService(token="t")
    stub = _StubAPI(draft_notes)
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service, stub


class TestInlineShapeGate:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate_immediate(tmp_path, monkeypatch)
        self.monkeypatch = monkeypatch
        # Own MR → the prose shape gate is a no-op; isolates this gate.
        from teatree.cli.review import shape_gate as shape_mod  # noqa: PLC0415

        monkeypatch.setattr(shape_mod, "fetch_mr_author", lambda api, encoded_repo, mr: _AUTHOR_ALICE)

    def test_mr_level_draft_refused_when_inline_drafts_exist(self) -> None:
        """RED → GREEN: a general draft note with a pending inline draft is refused."""
        service, stub = _service_with_stub([_INLINE_DRAFT])
        body = "Overall this looks fine, approving."

        msg, code = service.post_draft_note("org/repo", 7, body)

        assert code == 1, f"expected refuse, got code={code} msg={msg!r}"
        assert "already has 1 inline draft" in msg
        assert "--force-general" in msg
        assert not any(c[0] == "post_json" for c in stub.calls), "gate must block BEFORE any GitLab POST"

    def test_mr_level_draft_allowed_when_no_inline_drafts(self) -> None:
        """The normal flow: a general note with no pending inline drafts posts."""
        service, stub = _service_with_stub([])
        body = "Overall this looks fine, approving."

        msg, code = service.post_draft_note("org/repo", 7, body)

        assert code == 0, f"general note must pass with no inline drafts: {msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_inline_draft_never_blocked(self) -> None:
        """Posting an inline draft is never blocked, even with prior inline drafts."""
        service, stub = _service_with_stub([_INLINE_DRAFT])
        self.monkeypatch.setattr(
            "teatree.cli.review.service.resolve_inline_position",
            lambda api, encoded, mr, file, line: ({"new_path": file, "new_line": line}, ""),
        )
        msg, code = service.post_draft_note("org/repo", 7, "consider extracting a helper here", file="b.py", line=3)

        assert code == 0, f"inline draft must pass: {msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_force_general_overrides(self) -> None:
        """``force_general`` lets a genuinely MR-wide note through despite inline drafts."""
        service, stub = _service_with_stub([_INLINE_DRAFT])
        body = "Overall this looks fine, approving."

        msg, code = service.post_draft_note("org/repo", 7, body, force_general=True)

        assert code == 0, f"override must let the post proceed: {msg!r}"
        assert any(c[0] == "post_json" for c in stub.calls)
