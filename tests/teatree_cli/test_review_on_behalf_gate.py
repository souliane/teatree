"""The on-behalf pre-gate is enforced on every colleague-posting CLI method (#960).

``ReviewService`` is the second on-behalf chokepoint (alongside
``_BaseReplier``) — its ``post_comment``, ``post_draft_note``,
``publish_draft_notes``, ``reply_to_discussion``, ``resolve_discussion``,
``update_note``, and ``delete_discussion`` methods all publish on the
user's identity to a GitLab MR. They route through the same
satisfiable recorded-approval gate as the reply transport: gate ON +
no approval → refuse without posting (and surface the
approve-on-behalf invocation that satisfies it); gate ON + recorded
:class:`OnBehalfApproval` → publish and consume the row; gate OFF →
publish.

Pure-read / pre-publication methods (``list_draft_notes``,
``delete_draft_note``) are NOT on-behalf posts and remain ungated —
``delete_draft_note`` removes the *user's own* unpublished draft, no
colleague sees it. ``delete_discussion`` is different: it removes a
*published* (colleague-visible) note and IS gated.

The companion ``t3 review approve-on-behalf`` CLI command is the
no-TTY satisfier — its end-to-end behaviour is also exercised here so
the gated method ↔ approval recording loop is verified in one suite.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import ReviewService
from teatree.core.models import OnBehalfApproval

pytestmark = pytest.mark.django_db

_runner = CliRunner()


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f"[teatree]\nask_before_post_on_behalf = {'true' if on else 'false'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


class _StubAPI:
    """In-memory stand-in for ``GitLabAPI`` — records every network call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        return {"id": 1, "notes": [{"type": "DiffNote"}]}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        return 200

    def put_status(self, endpoint: str, payload: object | None = None) -> int:
        self.calls.append(("put_status", endpoint, payload))
        return 200

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        return []

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint, None))
        return 204


def _service_with_stub() -> tuple[ReviewService, _StubAPI]:
    service = ReviewService(token="t")
    stub = _StubAPI()
    # Replace the lazy API factory so no real GitLab call is attempted.
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service, stub


class TestReviewServicePostCommentGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_post_comment_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm")

        assert code == 1
        assert "approve-on-behalf" in msg
        # The HTTP call MUST NOT have happened.
        assert stub.calls == []

    def test_post_comment_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="post_comment", approver_id="souliane")
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm")

        assert code == 0
        assert "OK" in msg
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_post_comment_proceeds_when_gate_off(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=False)
        service, stub = _service_with_stub()

        _, code = service.post_comment("org/repo", 7, "lgtm")

        assert code == 0
        assert any(c[0] == "post_json" for c in stub.calls)


class TestReviewServicePostDraftNoteGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_post_draft_note_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.post_draft_note("org/repo", 7, "nit")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    def test_post_draft_note_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="post_draft_note", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code = service.post_draft_note("org/repo", 7, "nit")
        assert code == 0


class TestReviewServicePublishDraftsGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_publish_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.publish_draft_notes("org/repo", 7)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    def test_publish_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="publish_draft_notes", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code = service.publish_draft_notes("org/repo", 7)
        assert code == 0


class TestReviewServiceReplyToDiscussionGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_reply_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.reply_to_discussion("org/repo", 7, "d1", "thanks")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    def test_reply_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="reply_to_discussion", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code = service.reply_to_discussion("org/repo", 7, "d1", "thanks")
        assert code == 0


class TestReviewServiceResolveDiscussionGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_resolve_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.resolve_discussion("org/repo", 7, "d1")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    def test_resolve_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="resolve_discussion", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code = service.resolve_discussion("org/repo", 7, "d1")
        assert code == 0


class TestReviewServiceUpdateNoteGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_update_note_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.update_note("org/repo", 7, 99, "edited")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    def test_update_note_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="update_note", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code = service.update_note("org/repo", 7, 99, "edited")
        assert code == 0


class TestReviewServiceDeleteDiscussionGated:
    """Deleting a *published* discussion is a colleague-visible mutation — gated.

    Mirrors :class:`TestReviewServiceUpdateNoteGated` exactly: the action
    routes through the on-behalf gate just like ``update_note``, because
    removing a published comment under the user's identity is as visible
    to colleagues as editing one. Distinct from ``delete_draft_note``
    (ungated): a draft is pre-publication and not colleague-visible.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_delete_discussion_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        msg, code = service.delete_discussion("org/repo", 7, 99)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    def test_delete_discussion_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        OnBehalfApproval.record(target="org/repo!7", action="delete_discussion", approver_id="souliane")
        service, stub = _service_with_stub()

        msg, code = service.delete_discussion("org/repo", 7, 99)

        assert code == 0
        assert "OK" in msg
        assert any(c[0] == "delete" for c in stub.calls)

    def test_delete_discussion_proceeds_when_gate_off(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=False)
        service, stub = _service_with_stub()

        _, code = service.delete_discussion("org/repo", 7, 99)

        assert code == 0
        assert any(c[0] == "delete" for c in stub.calls)


class TestReviewServiceReadMethodsNotGated:
    """Pure-read methods (no on-behalf publish) MUST NOT be gated."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_list_draft_notes_runs_even_with_gate_on(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        _, code = service.list_draft_notes("org/repo", 7)
        assert code == 0
        # The list call hit the API — it was not blocked.
        assert any(c[0] == "get_json" for c in stub.calls)

    def test_delete_draft_note_runs_even_with_gate_on(self) -> None:
        """Deleting one's own draft (pre-publication) is not an on-behalf colleague post."""
        _gate(self.tmp_path, self.monkeypatch, on=True)
        service, stub = _service_with_stub()

        _, code = service.delete_draft_note("org/repo", 7, 99)
        assert code == 0
        assert any(c[0] == "delete" for c in stub.calls)


# Sanity: end-to-end, the service uses the gate helper at the chokepoint —
# not in the typer command wrapper. Patch the helper at its source module
# (``teatree.core.on_behalf_gate_recorded``) — the CLI imports it lazily
# inside ``_check_on_behalf`` to keep the cli module importable before
# ``django.setup()`` runs, so patching there is the canonical surface.
class TestReviewServiceGateIntegration:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate(tmp_path, monkeypatch, on=False)  # gate off — irrelevant here
        self.monkeypatch = monkeypatch

    def test_post_comment_calls_require_on_behalf_approval(self) -> None:
        from teatree.core import on_behalf_gate_recorded as gate_mod  # noqa: PLC0415

        called: list[tuple[str, str]] = []

        def _fake_require(*, target: str, action: str) -> None:
            called.append((target, action))

        with patch.object(gate_mod, "require_on_behalf_approval", _fake_require):
            service, _stub = _service_with_stub()
            service.post_comment("org/repo", 7, "lgtm")

        assert called == [("org/repo!7", "post_comment")]


class TestApproveOnBehalfCommand:
    """``t3 review approve-on-behalf`` is the no-TTY satisfier (#960)."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate(tmp_path, monkeypatch, on=True)

    def test_records_an_approval_row(self) -> None:
        result = _runner.invoke(
            app,
            ["review", "approve-on-behalf", "org/repo!7", "post_comment", "--approver", "souliane"],
        )
        assert result.exit_code == 0, result.output
        assert "OK recorded approval" in result.output
        approval = OnBehalfApproval.objects.get(target="org/repo!7", action="post_comment")
        assert approval.approver_id == "souliane"
        assert approval.consumed_at is None

    def test_refuses_a_maker_approver(self) -> None:
        result = _runner.invoke(
            app,
            ["review", "approve-on-behalf", "org/repo!7", "post_comment", "--approver", "coding-agent"],
        )
        assert result.exit_code == 1
        assert "Refused" in result.output
        assert OnBehalfApproval.objects.count() == 0

    def test_end_to_end_recorded_approval_satisfies_post_comment(self) -> None:
        """Record an approval via the CLI; the next ``post_comment`` then proceeds."""
        record = _runner.invoke(
            app,
            ["review", "approve-on-behalf", "org/repo!7", "post_comment", "--approver", "souliane"],
        )
        assert record.exit_code == 0, record.output

        # Gate still ON — but the recorded approval now satisfies the next call.
        service, stub = _service_with_stub()
        _, code = service.post_comment("org/repo", 7, "lgtm")

        assert code == 0
        assert any(c[0] == "post_json" for c in stub.calls)
        # Single-use: the approval is now consumed; a second call fails.
        _, code2 = service.post_comment("org/repo", 7, "lgtm")
        assert code2 == 1
