"""``t3 review post-comment`` defaults to DRAFT — ``--live`` needs a Slack-recorded token (#1207).

The historical ``post-comment`` published live to GitLab on every
invocation. Per #1207 the default flips to a DRAFT (safe-by-default);
the live, colleague-visible path is gated on
a single-use, MR-URL-scoped
:class:`~teatree.core.models.live_post_approval.LivePostApproval`
minted by ``t3 review approve-live-post`` after the helper verifies a
recent Slack DM from the user containing an explicit approval phrase.

This suite exercises every leg of the acceptance criteria:

* default (no flag) creates a draft + emits a Slack DM with the link;
* ``--live`` without a recorded approval refuses with an actionable
    message pointing at ``approve-live-post``;
* ``approve-live-post`` validates Slack-side authenticity (author,
    recency, phrase), mints the token, and ``--live`` then succeeds;
* the token is single-use — a second ``--live`` on the same MR refuses;
* the token is MR-URL-scoped — an approval for !1 does NOT authorise
    a live post on !2;
* an expired token (older than the TTL) is treated as absent;
* a Slack message authored by anyone other than the configured user
    is refused;
* the approval-phrase matcher is whole-word — ``"go ahead"`` matches,
    ``"thumbs up"`` does NOT, and a negated form (``"do NOT go ahead"``)
    is refused too. See ``test_review_live_approval_phrase.py`` for the
    full whole-word/negation matrix.
"""

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import ReviewService
from teatree.config import OnBehalfPostMode
from teatree.core.models import BotPing, ConfigSetting, LivePostApproval

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_runner = CliRunner()


def _write_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, user_id: str = "U-OPERATOR") -> None:
    """Pin the active config to a known ``slack_user_id`` + IMMEDIATE on-behalf gate.

    ``slack_user_id`` is raw non-UserSettings config and keeps its TOML home;
    ``on_behalf_post_mode`` is DB-home (#1775) so it resolves only from the
    ``ConfigSetting`` store — staging it via TOML would be a no-op on read.
    """
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "{user_id}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    ConfigSetting.objects.set_value("on_behalf_post_mode", OnBehalfPostMode.IMMEDIATE.value)


def _wire_notify_backend(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)
    return backend


class _StubAPI:
    """In-memory ``GitLabAPI`` stand-in returning the new default-draft shape."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        return {"id": 77, "notes": [{"type": "DiffNote"}], "line_code": "abc_10_10"}

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


def _service(monkeypatch: pytest.MonkeyPatch) -> tuple[ReviewService, _StubAPI]:
    service = ReviewService(token="t")
    stub = _StubAPI()
    monkeypatch.setattr(service, "_get_api", lambda: stub)
    return service, stub


class TestPostCommentDefaultsToDraft:
    """No ``--live`` flag → draft note + Slack DM (#1207 happy path)."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        self.backend = _wire_notify_backend(monkeypatch)
        self.svc, self.stub = _service(monkeypatch)

    def test_default_creates_a_draft_note(self) -> None:
        msg, code = self.svc.post_comment("org/repo", 7, "lgtm")

        assert code == 0, msg
        assert "draft_note_id=77" in msg
        # The HTTP call landed on the draft-notes endpoint, not /discussions.
        post_endpoints = [endpoint for kind, endpoint, _ in self.stub.calls if kind == "post_json"]
        assert any("draft_notes" in ep for ep in post_endpoints), f"expected draft_notes hit, got {post_endpoints!r}"

    def test_default_emits_slack_dm_with_link(self) -> None:
        _, code = self.svc.post_comment("org/repo", 7, "lgtm")
        assert code == 0
        assert BotPing.objects.filter(idempotency_key__startswith="post_comment_draft:org/repo!7:").exists()


class TestPostCommentLiveRefusedWithoutToken:
    """``--live`` without a recorded approval refuses with an actionable message."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        _wire_notify_backend(monkeypatch)
        self.svc, self.stub = _service(monkeypatch)

    def test_live_post_refused_without_approval(self) -> None:
        msg, code = self.svc.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 1
        assert "approve-live-post" in msg
        # No publish-shaped HTTP call lands when the gate refuses — the
        # shape gate's read-only ``get_json`` MR-author lookup is fine.
        assert all(kind != "post_json" for kind, _, _ in self.stub.calls)


class TestPostCommentLiveConsumesToken:
    """A recorded ``LivePostApproval`` satisfies ``--live`` exactly once."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        _wire_notify_backend(monkeypatch)
        self.svc, self.stub = _service(monkeypatch)

    def test_live_proceeds_with_recorded_approval(self) -> None:
        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

        msg, code = self.svc.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 0, msg
        assert "OK note_id=77" in msg

    def test_token_is_single_use(self) -> None:
        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

        _, code1 = self.svc.post_comment("org/repo", 7, "lgtm", live=True)
        assert code1 == 0
        msg2, code2 = self.svc.post_comment("org/repo", 7, "lgtm again", live=True)
        assert code2 == 1
        assert "approve-live-post" in msg2


class TestPostCommentLiveScopedToMr:
    """A token for !1 does NOT authorise a live post on !2."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        _wire_notify_backend(monkeypatch)
        self.svc, self.stub = _service(monkeypatch)

    def test_wrong_mr_token_does_not_authorise(self) -> None:
        LivePostApproval.record(mr_url="org/repo!1", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

        msg, code = self.svc.post_comment("org/repo", 2, "lgtm", live=True)

        assert code == 1
        assert "approve-live-post" in msg
        # The matching approval for !1 is still available (NOT consumed).
        assert LivePostApproval.objects.filter(mr_url="org/repo!1", consumed_at__isnull=True).exists()


class TestPostCommentLiveTokenExpiry:
    """A token older than the TTL window is treated as absent."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        _wire_notify_backend(monkeypatch)
        self.svc, self.stub = _service(monkeypatch)

    def test_expired_token_does_not_authorise(self) -> None:
        approval = LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")
        # Backdate the row beyond the 15-minute TTL.
        LivePostApproval.objects.filter(pk=approval.pk).update(created_at=timezone.now() - timedelta(minutes=20))

        msg, code = self.svc.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 1
        assert "approve-live-post" in msg


class TestApproveLivePostCommand:
    """``t3 review approve-live-post`` verifies the Slack DM, then mints the token."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def _wire_slack_backend(self, *, message: dict[str, object]) -> MagicMock:
        backend = MagicMock()
        backend.open_dm.return_value = "D-OPERATOR"
        backend.fetch_message.return_value = message
        # The CLI imports the backend lazily inside the command body so
        # ``django.setup()`` doesn't have to run at module import — patch
        # the canonical source.
        self.monkeypatch.setattr(
            "teatree.core.backend_factory.messaging_from_overlay",
            lambda: backend,
        )
        return backend

    def _fresh_ts(self) -> str:
        return f"{timezone.now().timestamp():.4f}"

    def test_records_a_live_post_approval(self) -> None:
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": "go ahead"})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", ts],
        )

        assert result.exit_code == 0, result.output
        assert "OK recorded live-post approval" in result.output
        row = LivePostApproval.objects.get(mr_url="org/repo!7")
        assert row.slack_ts == ts
        assert row.slack_user_id == "U-OPERATOR"
        assert row.consumed_at is None

    def test_refuses_when_slack_message_author_is_not_the_user(self) -> None:
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-IMPOSTER", "text": "go ahead"})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", ts],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert LivePostApproval.objects.count() == 0

    def test_refuses_when_approval_phrase_is_missing(self) -> None:
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": "thumbs up"})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", ts],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert "approval phrase" in result.output
        assert LivePostApproval.objects.count() == 0

    def test_accepts_go_ahead_phrase_case_insensitively(self) -> None:
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": "GO Ahead and post it"})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", ts],
        )

        assert result.exit_code == 0, result.output

    def test_refuses_stale_slack_message(self) -> None:
        # Older than the 15-minute TTL.
        stale_ts = f"{(timezone.now() - timedelta(minutes=30)).timestamp():.4f}"
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": "go ahead"})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", stale_ts],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert "expired" in result.output or "older than" in result.output

    def test_accepts_gitlab_url_form(self) -> None:
        """The CLI accepts a GitLab URL and canonicalises it to ``<repo>!<iid>``."""
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": "submit it"})

        result = _runner.invoke(
            app,
            [
                "review",
                "approve-live-post",
                "https://gitlab.com/org/repo/-/merge_requests/7",
                "--slack-ts",
                ts,
            ],
        )

        assert result.exit_code == 0, result.output
        # The persisted scope is the canonical token, matching what
        # ``post-comment --live <repo> <iid>`` resolves to.
        assert LivePostApproval.objects.filter(mr_url="org/repo!7").exists()
