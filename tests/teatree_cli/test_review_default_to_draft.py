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

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review import ReviewService
from teatree.cli.review.default_draft import notify_draft_created, resolve_reviewed_head_sha
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
        # The key is scoped to the MR + reviewed revision (a single
        # per-comment-free discriminator), so all draft comments on one MR
        # revision coalesce into a single DM. The bare ``_StubAPI`` returns no
        # diff_refs, so the discriminator degrades to the UTC-day fallback.
        assert BotPing.objects.filter(idempotency_key__startswith="post_comment_draft:org/repo!7:").exists()


class _RoundStubAPI(_StubAPI):
    """A ``_StubAPI`` that counts note ids AND serves an MR head SHA.

    Successive draft posts return distinct note ids (so each ``post-comment``
    call has a distinct result message — the shape the pre-fix per-comment
    digest fanned out to N DMs). ``head_sha`` is mutable so a test can simulate
    the author pushing a new revision between review rounds; an empty
    ``head_sha`` exercises the UTC-day fallback (no diff_refs resolvable).
    """

    def __init__(self, head_sha: str = "sha-round-1") -> None:
        super().__init__()
        self._next_id = 100
        self.head_sha = head_sha

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        self.calls.append(("post_json", endpoint, payload))
        self._next_id += 1
        return {"id": self._next_id, "notes": [{"type": "DiffNote"}], "line_code": f"abc_{self._next_id}_10"}

    def get_json(self, endpoint: str) -> object:
        self.calls.append(("get_json", endpoint, None))
        tail = endpoint.rsplit("/merge_requests/", 1)[-1]
        if "/merge_requests/" in endpoint and tail.isdigit():  # the MR-detail GET
            return {"diff_refs": {"head_sha": self.head_sha, "base_sha": "b", "start_sha": "s"}}
        return []


class TestDraftCommentNotificationIsOneTerseLinePerMr:
    """Draft comments on one MR revision → ONE terse, clickable DM (not N essays)."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        self.backend = _wire_notify_backend(monkeypatch)
        self.svc = ReviewService(token="t")
        self.stub = _RoundStubAPI()
        monkeypatch.setattr(self.svc, "_get_api", lambda: self.stub)
        # Pin the forge base URL so the MR link is deterministic across hosts.
        monkeypatch.setattr(self.svc, "_resolve_base_url", lambda: "https://gitlab.example.com/api/v4")

    def test_many_comments_in_one_pass_coalesce_to_one_message(self) -> None:
        for note in ("looks good", "clean helper here", "nice separation"):
            _, code = self.svc.post_comment("org/repo", 6521, note)
            assert code == 0

        rows = BotPing.objects.filter(idempotency_key__startswith="post_comment_draft:org/repo!6521:")
        assert rows.count() == 1, [r.idempotency_key for r in rows]
        assert rows.get().idempotency_key == "post_comment_draft:org/repo!6521:sha-round-1"

    def test_new_round_new_head_sha_re_notifies(self) -> None:
        # Round 1 — two comments against sha-round-1 → one DM.
        for note in ("looks good", "one more"):
            _, code = self.svc.post_comment("org/repo", 6521, note)
            assert code == 0
        # Author pushes a fix; the reviewed head moves. Round 2 must re-notify —
        # a bare per-MR key would suppress it forever (SENT rows never expire).
        self.stub.head_sha = "sha-round-2"
        _, code = self.svc.post_comment("org/repo", 6521, "round-two finding")
        assert code == 0

        keys = sorted(
            r.idempotency_key
            for r in BotPing.objects.filter(idempotency_key__startswith="post_comment_draft:org/repo!6521:")
        )
        assert keys == [
            "post_comment_draft:org/repo!6521:sha-round-1",
            "post_comment_draft:org/repo!6521:sha-round-2",
        ], keys

    def test_falls_back_to_utc_day_when_head_sha_unavailable(self) -> None:
        self.stub.head_sha = ""  # unresolvable head → UTC-day discriminator
        _, code = self.svc.post_comment("org/repo", 6521, "looks good")
        assert code == 0

        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        assert BotPing.objects.filter(idempotency_key=f"post_comment_draft:org/repo!6521:{today}").exists()

    def test_message_is_a_single_clickable_line(self) -> None:
        _, code = self.svc.post_comment("org/repo", 6521, "looks good")
        assert code == 0

        row = BotPing.objects.get(idempotency_key="post_comment_draft:org/repo!6521:sha-round-1")
        # One terse line: no per-comment breakdown, no publish/discard essay.
        assert row.text == (
            "Posted draft comments on [org/repo!6521](https://gitlab.example.com/org/repo/-/merge_requests/6521)"
        )
        assert "\n" not in row.text
        for essay_marker in ("Publish:", "Discard:", "Body:", "draft_note_id", "default-draft gate"):
            assert essay_marker not in row.text

    def test_delivered_slack_message_has_a_clickable_mr_link(self) -> None:
        _, code = self.svc.post_comment("org/repo", 6521, "looks good")
        assert code == 0

        # The delivered Slack payload rewrites the markdown link into Slack
        # mrkdwn ``<url|label>`` — a clickable MR reference.
        sent = " ".join(str(a) for a in self.backend.post_message.call_args.args)
        sent += " ".join(f"{k}={v}" for k, v in self.backend.post_message.call_args.kwargs.items())
        assert "<https://gitlab.example.com/org/repo/-/merge_requests/6521|org/repo!6521>" in sent


class _RaisingAPI:
    """A ``GitLabAPI`` stand-in whose MR-detail GET raises the given error.

    Exercises ``resolve_reviewed_head_sha``'s best-effort degrade — a
    transport/status error (``httpx.HTTPError``) or a malformed body
    (``ValueError`` from ``response.json()``) must yield ``""``, not raise.
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_json(self, endpoint: str) -> object:
        raise self._exc


class TestResolveReviewedHeadShaDegradesOnLookupFailure:
    """A failed head-SHA lookup returns ``""`` → the notify key degrades to the UTC day."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        self.backend = _wire_notify_backend(monkeypatch)

    @pytest.mark.parametrize("exc", [httpx.HTTPError("boom"), ValueError("bad json")])
    def test_lookup_failure_degrades_notify_key_to_utc_day(self, exc: Exception) -> None:
        head_sha = resolve_reviewed_head_sha(_RaisingAPI(exc), "org/repo", 6521)
        assert head_sha == ""

        notify_draft_created(
            repo="org/repo",
            mr=6521,
            mr_url="https://gitlab.example.com/org/repo/-/merge_requests/6521",
            reviewed_head_sha=head_sha,
        )

        today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        assert BotPing.objects.filter(idempotency_key=f"post_comment_draft:org/repo!6521:{today}").exists()


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
