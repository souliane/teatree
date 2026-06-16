"""The tri-state on-behalf pre-gate is enforced on every colleague-posting CLI method (#960).

``ReviewService`` is the second on-behalf chokepoint (alongside
``_BaseReplier``) — its ``post_comment``, ``post_draft_note``,
``publish_draft_notes``, ``reply_to_discussion``, ``resolve_discussion``,
``update_note``, and ``delete_discussion`` methods all publish on the
user's identity to a GitLab MR. They route through the same
satisfiable ``on_behalf_post_mode`` gate as the reply transport.

Behavior per mode (parametrised across every gated class below):

*   :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE` → publish (no
    approval needed).
*   :attr:`~teatree.config.OnBehalfPostMode.ASK` → refuse without a
    recorded :class:`OnBehalfApproval`, publish with one.
*   :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` (new default)
    → ``post_draft_note`` publishes autonomously and records a
    ``BotPing`` row for the user DM; every other gated method behaves
    identically to ASK (colleague-visible mutations always need the
    recorded approval).

Pure-read / pre-publication methods (``list_draft_notes``,
``delete_draft_note``) are NOT on-behalf posts and remain ungated —
``delete_draft_note`` removes the *user's own* unpublished draft, no
colleague sees it. ``delete_discussion`` is different: it removes a
*published* (colleague-visible) note and IS gated.

The companion ``t3 review approve-on-behalf`` CLI command is the
no-TTY satisfier — its end-to-end behaviour is also exercised here so
the gated method ↔ approval recording loop is verified in one suite.
"""

from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import ReviewService
from teatree.config import OnBehalfPostMode
from teatree.core.models import BotPing, OnBehalfApproval

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_runner = CliRunner()


def _http_404() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gitlab.example.com/api/v4/x")
    response = httpx.Response(HTTPStatus.NOT_FOUND, request=request)
    return httpx.HTTPStatusError("not found", request=request, response=response)


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: OnBehalfPostMode) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\non_behalf_post_mode = "{mode.value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


# Modes under which a non-draft colleague-visible action is blocked.
_BLOCKING_MODES = [OnBehalfPostMode.ASK, OnBehalfPostMode.DRAFT_OR_ASK]


class _StubAPI:
    """In-memory stand-in for ``GitLabAPI`` — records every network call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self._deleted_ids: set[str] = set()

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        # ``line_code`` keeps the draft-notes anchor check happy on the
        # new default-draft ``post_comment`` path (#1207); the discussions
        # endpoint ignores it, so the same shape serves both branches.
        return {"id": 1, "notes": [{"type": "DiffNote", "id": 1}], "line_code": "abc123_10_10"}

    def post_status(self, endpoint: str) -> int:
        self.calls.append(("post_status", endpoint, None))
        return 200

    def put_status(self, endpoint: str, payload: object | None = None) -> int:
        self.calls.append(("put_status", endpoint, payload))
        return 200

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        # Verify-after-post (#2081) reads the artifact back: confirm it landed.
        # A note whose delete already succeeded reads back as 404 (gone), which
        # is exactly what ``verify_note_deleted`` requires for a clean delete.
        last = endpoint.rstrip("/").rsplit("/", 1)[-1]
        if last.isdigit():
            if last in self._deleted_ids:
                raise _http_404()
            return {"id": int(last), "resolvable": True, "resolved": True}
        if endpoint.endswith("/approvals"):
            return {"approved_by": [{"user": {"username": "souliane"}}]}
        if last == "draft_notes":
            return []  # all drafts published
        if last == "notes":
            return [{"id": 99, "author": {"username": "souliane"}}]
        if "discussions/" in endpoint:
            return {"notes": [{"resolvable": True, "resolved": True}]}
        return []

    def delete(self, endpoint: str) -> int:
        self.calls.append(("delete", endpoint, None))
        self._deleted_ids.add(endpoint.rstrip("/").rsplit("/", 1)[-1])
        return 204


def _service_with_stub() -> tuple[ReviewService, _StubAPI]:
    service = ReviewService(token="t")
    stub = _StubAPI()
    # Replace the lazy API factory so no real GitLab call is attempted.
    service._get_api = lambda: stub  # type: ignore[method-assign]
    return service, stub


class TestReviewServicePostCommentGated:
    """``post_comment`` default-draft path: drafts bypass the gate under EVERY mode (#draft-bypass).

    The default (live=False) path routes through ``post_draft_note``,
    which is colleague-INVISIBLE and therefore exempt from the on-behalf
    gate under every mode — under ``DRAFT_OR_ASK`` AND ``ASK`` the draft
    auto-publishes with a user DM, under ``IMMEDIATE`` it publishes with
    no DM. No recorded approval is ever required for the draft path. The
    ``--live`` path stays gated on the ``post_comment`` action.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_post_comment_default_auto_drafts_under_ask_no_approval(self) -> None:
        """ANTI-VACUITY: under ASK with NO approval the default draft path SUCCEEDS.

        Pre-fix this BLOCKed (the bug: a colleague-invisible draft needed
        approval under ASK). With the fix the draft auto-publishes without
        any recorded approval — a draft is never colleague-visible.
        """
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.ASK)
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm")

        assert code == 0, msg
        # The draft-note publish DID happen, on ``/draft_notes``.
        post_endpoints = [endpoint for kind, endpoint, _ in stub.calls if kind == "post_json"]
        assert any("draft_notes" in ep for ep in post_endpoints), f"expected draft_notes hit, got {post_endpoints!r}"
        # No approval was recorded or consumed — the draft never needed one.
        assert not OnBehalfApproval.objects.exists()

    def test_post_comment_default_auto_drafts_under_draft_or_ask(self) -> None:
        """Under DRAFT_OR_ASK the default draft path auto-publishes (the #1207 default flip)."""
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.DRAFT_OR_ASK)
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm")

        assert code == 0, msg
        # The draft-note publish lands on ``/draft_notes`` (not ``/discussions``).
        post_endpoints = [endpoint for kind, endpoint, _ in stub.calls if kind == "post_json"]
        assert any("draft_notes" in ep for ep in post_endpoints), f"expected draft_notes hit, got {post_endpoints!r}"

    def test_post_comment_default_proceeds_under_ask_without_approval(self) -> None:
        """No approval needed under ASK — the draft path is exempt, it just proceeds."""
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.ASK)
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm")

        assert code == 0, msg
        assert any(c[0] == "post_json" for c in stub.calls)

    def test_post_comment_default_proceeds_under_immediate(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)
        service, stub = _service_with_stub()

        _, code = service.post_comment("org/repo", 7, "lgtm")

        assert code == 0
        assert any(c[0] == "post_json" for c in stub.calls)

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_post_comment_live_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        """The ``--live`` branch keeps the gate and names the one-step ``authorize`` (#126)."""
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 1
        # The unified refusal names the single ``t3 review authorize`` command
        # (the #126 collapse) rather than the old two-command dance.
        assert "authorize" in msg
        # The HTTP publish MUST NOT have happened.
        assert all(kind != "post_json" for kind, _, _ in stub.calls)

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_post_comment_live_still_needs_live_post_token_with_on_behalf_approval(
        self, mode: OnBehalfPostMode
    ) -> None:
        """A ``post_comment`` on-behalf approval alone does NOT satisfy ``--live``.

        Both gates must be satisfied: the on-behalf approval (#960) AND the
        Slack-recorded LivePostApproval (#1207). With only the former, the
        ``--live`` call still refuses with the ``approve-live-post`` message.
        """
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        OnBehalfApproval.record(target="org/repo!7", action="post_comment", approver_id="souliane")
        service, _stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 1
        assert "approve-live-post" in msg

    def test_post_comment_live_under_immediate_still_needs_live_post_token(self) -> None:
        """``--live`` is gated on the Slack-recorded LivePostApproval even under IMMEDIATE.

        The on-behalf gate (#960) and the live-post gate (#1207) are
        independent: IMMEDIATE relaxes only the on-behalf pre-ask, the
        ``--live`` colleague-visible publish still needs the Slack-DM-
        verified token.
        """
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)
        service, _stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 1
        assert "approve-live-post" in msg

    def test_post_comment_live_proceeds_with_both_approvals(self) -> None:
        """``--live`` publishes when both the on-behalf approval AND the LivePostApproval are recorded."""
        from teatree.core.models import LivePostApproval  # noqa: PLC0415

        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.ASK)
        OnBehalfApproval.record(target="org/repo!7", action="post_comment", approver_id="souliane")
        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")
        service, stub = _service_with_stub()

        msg, code = service.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 0, msg
        # The live publish lands on ``/discussions`` (or ``/notes``), NOT ``/draft_notes``.
        post_endpoints = [endpoint for kind, endpoint, _ in stub.calls if kind == "post_json"]
        assert any(("draft_notes" not in ep) and ("notes" in ep) for ep in post_endpoints), (
            f"expected a live publish to /notes or /discussions, got {post_endpoints!r}"
        )


class TestReviewServicePostDraftNoteGated:
    """``post_draft_note`` is the draft-form action — EXEMPT from the gate under every mode."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def test_post_draft_note_auto_drafts_under_ask_no_approval(self) -> None:
        """ANTI-VACUITY: under ASK with NO recorded approval the draft note SUCCEEDS.

        Pre-fix this BLOCKed (the bug). With the fix a draft is exempt
        from the gate under ASK exactly as under DRAFT_OR_ASK: it
        auto-publishes and records the user-DM ``BotPing`` — no approval.
        """
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.ASK)
        service, stub = _service_with_stub()

        msg, code = service.post_draft_note("org/repo", 7, "nit")

        assert code == 0, msg
        assert any(c[0] == "post_json" for c in stub.calls), "The draft note publish must fire"
        # The autodraft user-DM receipt is recorded under ASK too.
        ping = BotPing.objects.get(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note")
        assert ping.kind == BotPing.Kind.INFO
        # No approval was recorded or consumed.
        assert not OnBehalfApproval.objects.exists()

    def test_post_draft_note_auto_drafts_under_draft_or_ask(self) -> None:
        """Under DRAFT_OR_ASK, post_draft_note publishes autonomously + records a BotPing."""
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.DRAFT_OR_ASK)
        service, stub = _service_with_stub()

        _, code = service.post_draft_note("org/repo", 7, "nit")

        assert code == 0
        assert any(c[0] == "post_json" for c in stub.calls), "The draft note publish must fire"
        ping = BotPing.objects.get(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note")
        assert ping.kind == BotPing.Kind.INFO

    def test_post_draft_note_passes_under_immediate(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)
        service, stub = _service_with_stub()

        _, code = service.post_draft_note("org/repo", 7, "nit")
        assert code == 0
        assert any(c[0] == "post_json" for c in stub.calls)


class TestReviewServicePublishDraftsGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_publish_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        service, stub = _service_with_stub()

        msg, code = service.publish_draft_notes("org/repo", 7)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_publish_proceeds_with_recorded_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        OnBehalfApproval.record(target="org/repo!7", action="publish_draft_notes", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code = service.publish_draft_notes("org/repo", 7)
        assert code == 0


class TestReviewServiceReplyToDiscussionGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_reply_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        service, stub = _service_with_stub()

        msg, code = service.reply_to_discussion("org/repo", 7, "d1", "thanks")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_reply_proceeds_with_recorded_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        OnBehalfApproval.record(target="org/repo!7", action="reply_to_discussion", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code = service.reply_to_discussion("org/repo", 7, "d1", "thanks")
        assert code == 0


class TestReviewServiceResolveDiscussionGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_resolve_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        service, stub = _service_with_stub()

        msg, code = service.resolve_discussion("org/repo", 7, "d1")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_resolve_proceeds_with_recorded_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        OnBehalfApproval.record(target="org/repo!7", action="resolve_discussion", approver_id="souliane")
        service, _stub = _service_with_stub()

        _, code = service.resolve_discussion("org/repo", 7, "d1")
        assert code == 0


class TestReviewServiceUpdateNoteGated:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_update_note_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        service, stub = _service_with_stub()

        msg, code = service.update_note("org/repo", 7, 99, "edited")

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_update_note_proceeds_with_recorded_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
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

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_delete_discussion_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        service, stub = _service_with_stub()

        msg, code = service.delete_discussion("org/repo", 7, 99)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_delete_discussion_proceeds_with_recorded_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        OnBehalfApproval.record(target="org/repo!7", action="delete_discussion", approver_id="souliane")
        service, stub = _service_with_stub()

        msg, code = service.delete_discussion("org/repo", 7, 99)

        assert code == 0
        assert "OK" in msg
        assert any(c[0] == "delete" for c in stub.calls)

    def test_delete_discussion_proceeds_under_immediate(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)
        service, stub = _service_with_stub()

        _, code = service.delete_discussion("org/repo", 7, 99)

        assert code == 0
        assert any(c[0] == "delete" for c in stub.calls)


class TestReviewServiceDeleteIssueNoteGated:
    """Deleting a published ISSUE/work-item note is a colleague-visible mutation — gated.

    The issue/work-item twin of :class:`TestReviewServiceDeleteDiscussionGated`.
    The on-behalf scope is ``<repo>#<issue>`` (``#``, not ``!``), so an MR
    approval can never satisfy an issue-note delete and vice versa.
    """

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_delete_issue_note_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        service, stub = _service_with_stub()

        msg, code = service.delete_issue_note("org/repo", 8568, 99)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_delete_issue_note_proceeds_with_recorded_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        OnBehalfApproval.record(target="org/repo#8568", action="delete_issue_note", approver_id="souliane")
        service, stub = _service_with_stub()

        msg, code = service.delete_issue_note("org/repo", 8568, 99)

        assert code == 0, msg
        assert "OK" in msg
        delete_endpoints = [endpoint for kind, endpoint, _ in stub.calls if kind == "delete"]
        assert any("issues/8568/notes/99" in ep for ep in delete_endpoints), (
            f"expected the issue-notes delete endpoint, got {delete_endpoints!r}"
        )

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_mr_scoped_approval_does_not_satisfy_issue_note_delete(self, mode: OnBehalfPostMode) -> None:
        """ANTI-VACUITY: a ``<repo>!<iid>`` MR approval must NOT unlock the issue-note delete.

        Without the ``#`` scope separation an approval recorded for the
        same-numbered MR would satisfy the issue-note delete — exactly the
        confusion the distinct target prevents. The delete must still BLOCK.
        """
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        OnBehalfApproval.record(target="org/repo!8568", action="delete_issue_note", approver_id="souliane")
        service, stub = _service_with_stub()

        msg, code = service.delete_issue_note("org/repo", 8568, 99)

        assert code == 1
        assert "approve-on-behalf" in msg
        assert stub.calls == []

    def test_delete_issue_note_proceeds_under_immediate(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)
        service, stub = _service_with_stub()

        _, code = service.delete_issue_note("org/repo", 8568, 99)

        assert code == 0
        assert any(c[0] == "delete" for c in stub.calls)

    def test_recorded_issue_approval_via_cli_satisfies_delete(self) -> None:
        """End-to-end: ``approve-on-behalf <repo>#<issue> delete_issue_note`` unlocks the delete."""
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.ASK)
        record = _runner.invoke(
            app,
            ["review", "approve-on-behalf", "org/repo#8568", "delete_issue_note", "--approver", "souliane"],
        )
        assert record.exit_code == 0, record.output

        service, stub = _service_with_stub()
        msg, code = service.delete_issue_note("org/repo", 8568, 99)

        assert code == 0, msg
        assert any(c[0] == "delete" for c in stub.calls)
        # Single-use: a second delete blocks again.
        _, code2 = service.delete_issue_note("org/repo", 8568, 99)
        assert code2 == 1


class TestReviewServiceReadMethodsNotGated:
    """Pure-read methods (no on-behalf publish) MUST NOT be gated."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_list_draft_notes_runs_even_when_blocked(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        service, stub = _service_with_stub()

        _, code = service.list_draft_notes("org/repo", 7)
        assert code == 0
        # The list call hit the API — it was not blocked.
        assert any(c[0] == "get_json" for c in stub.calls)

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_delete_draft_note_runs_even_when_blocked(self, mode: OnBehalfPostMode) -> None:
        """Deleting one's own draft (pre-publication) is not an on-behalf colleague post."""
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
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
        _gate(tmp_path, monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)  # gate off — irrelevant here
        self.monkeypatch = monkeypatch

    def test_post_comment_default_calls_require_with_post_draft_note(self) -> None:
        """The default-draft path routes through ``post_draft_note``'s gate (#1207)."""
        from teatree.core import on_behalf_gate_recorded as gate_mod  # noqa: PLC0415

        called: list[tuple[str, str]] = []

        def _fake_require[T](*, target: str, action: str, publish: Callable[[], T]) -> T:
            called.append((target, action))
            return publish()

        with patch.object(gate_mod, "require_on_behalf_approval", _fake_require):
            service, _stub = _service_with_stub()
            service.post_comment("org/repo", 7, "lgtm")

        # Default path => the on-behalf action consumed is the draft-form
        # ``post_draft_note``, NOT ``post_comment``. ``post_comment`` is
        # reserved for the ``--live`` colleague-visible branch.
        assert called == [("org/repo!7", "post_draft_note")]

    def test_post_comment_live_calls_require_with_post_comment(self) -> None:
        """The ``--live`` branch still gates on the ``post_comment`` on-behalf action."""
        from teatree.core import on_behalf_gate_recorded as gate_mod  # noqa: PLC0415
        from teatree.core.models import LivePostApproval  # noqa: PLC0415

        # The live publish reaches the atomic gate only past the LivePostApproval
        # authorization (#1207); record one so the post site (and thus the
        # consume) is reached.
        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

        called: list[tuple[str, str]] = []

        def _fake_require[T](*, target: str, action: str, publish: Callable[[], T]) -> T:
            called.append((target, action))
            return publish()

        with patch.object(gate_mod, "require_on_behalf_approval", _fake_require):
            service, _stub = _service_with_stub()
            service.post_comment("org/repo", 7, "lgtm", live=True)

        assert ("org/repo!7", "post_comment") in called


class TestApproveOnBehalfCommand:
    """``t3 review approve-on-behalf`` is the no-TTY satisfier (#960)."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate(tmp_path, monkeypatch, mode=OnBehalfPostMode.ASK)

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

    def test_end_to_end_recorded_approval_satisfies_visible_post(self) -> None:
        """Record an approval via the CLI; the next colleague-VISIBLE post then proceeds.

        Uses ``reply_to_discussion`` (a colleague-visible, single-chokepoint
        action) — drafts are exempt from the gate so they cannot exercise
        the recorded-approval satisfier. The recorded approval is single-use:
        the second visible post blocks again.
        """
        record = _runner.invoke(
            app,
            ["review", "approve-on-behalf", "org/repo!7", "reply_to_discussion", "--approver", "souliane"],
        )
        assert record.exit_code == 0, record.output

        # Gate still in ASK mode — but the recorded approval now satisfies the next call.
        service, stub = _service_with_stub()
        _, code = service.reply_to_discussion("org/repo", 7, "d1", "thanks")

        assert code == 0
        assert any(c[0] == "post_json" for c in stub.calls)
        # Single-use: the approval is now consumed; a second call fails.
        _, code2 = service.reply_to_discussion("org/repo", 7, "d1", "thanks")
        assert code2 == 1
