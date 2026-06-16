r"""Eval matrix for the live-post-approval authorization gate (#1207, #126).

The gate-over-deny lockout this guards against: ``t3 review
approve-live-post`` historically minted the live-post token ONLY from a
fresh Slack-DM ts carrying one of three hard-coded phrases. A user who
approved the post four times in chat + an AskUserQuestion AND recorded a
durable on-behalf approval token still could not get a live post out —
the gate fail-closed against every legitimate authorization channel that
was not the one narrow Slack-ts path.

This matrix asserts the four authorization scenarios as a unit:

* a recorded on-behalf approval (the durable human authorization minted
    by ``t3 review approve-on-behalf <scope> post_comment``) is sufficient
    to mint the live-post token — no Slack ts required;
* a natural approval phrase in the Slack DM (``"post the findings"`` /
    ``"post them"`` / ``"approved"`` / ``"ship it"``) is accepted;
* the original explicit phrase (``"go ahead"`` etc.) still works;
* NO approval of any kind → still blocked (the genuine guard).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app
from teatree.config import OnBehalfPostMode
from teatree.core.models.live_post_approval import LivePostApproval
from teatree.core.models.on_behalf_approval import OnBehalfApproval

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_runner = CliRunner()


def _write_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, user_id: str = "U-OPERATOR") -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\nslack_user_id = "{user_id}"\non_behalf_post_mode = "{OnBehalfPostMode.IMMEDIATE.value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


class TestLivePostApprovalAuthorizationMatrix:
    """The four authorization scenarios for minting a live-post token."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch)
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def _wire_slack_backend(self, *, message: dict[str, object] | None) -> MagicMock:
        backend = MagicMock()
        backend.open_dm.return_value = "D-OPERATOR"
        backend.fetch_message.return_value = message
        self.monkeypatch.setattr(
            "teatree.core.backend_factory.messaging_from_overlay",
            lambda: backend,
        )
        return backend

    def _fresh_ts(self) -> str:
        return f"{timezone.now().timestamp():.4f}"

    # ── ALLOW: recorded on-behalf approval mints the token ──────────

    def test_recorded_on_behalf_approval_mints_live_token(self) -> None:
        # The durable human authorization (id=N) IS sufficient — no Slack
        # ts, no phrase. This is the lockout the issue describes.
        OnBehalfApproval.record(target="org/repo!7", action="post_comment", approver_id="U-OPERATOR")

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--from-on-behalf"],
        )

        assert result.exit_code == 0, result.output
        assert "OK recorded live-post approval" in result.output
        row = LivePostApproval.objects.get(mr_url="org/repo!7")
        assert row.consumed_at is None

    def test_on_behalf_approval_accepts_gitlab_url_scope(self) -> None:
        OnBehalfApproval.record(target="org/repo!7", action="post_comment", approver_id="U-OPERATOR")

        result = _runner.invoke(
            app,
            [
                "review",
                "approve-live-post",
                "https://gitlab.com/org/repo/-/merge_requests/7",
                "--from-on-behalf",
            ],
        )

        assert result.exit_code == 0, result.output
        assert LivePostApproval.objects.filter(mr_url="org/repo!7").exists()

    def test_from_on_behalf_without_recorded_approval_is_blocked(self) -> None:
        # No on-behalf row for this scope → the durable-authorization
        # path cannot mint a token. Genuine guard preserved.
        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--from-on-behalf"],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert LivePostApproval.objects.count() == 0

    def test_on_behalf_approval_for_other_mr_does_not_authorize(self) -> None:
        OnBehalfApproval.record(target="org/repo!1", action="post_comment", approver_id="U-OPERATOR")

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!2", "--from-on-behalf"],
        )

        assert result.exit_code == 1
        assert LivePostApproval.objects.filter(mr_url="org/repo!2").count() == 0

    # ── ALLOW: natural approval wording in the Slack DM ─────────────

    @pytest.mark.parametrize(
        "phrase",
        ["post the findings", "post them", "approved", "ship it"],
    )
    def test_natural_approval_phrase_is_accepted(self, phrase: str) -> None:
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": phrase})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", ts],
        )

        assert result.exit_code == 0, result.output
        assert LivePostApproval.objects.filter(mr_url="org/repo!7").exists()

    # ── ALLOW: the original explicit phrase still works ─────────────

    @pytest.mark.parametrize("phrase", ["go ahead", "post live", "submit it"])
    def test_explicit_phrase_still_accepted(self, phrase: str) -> None:
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": phrase})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", ts],
        )

        assert result.exit_code == 0, result.output

    # ── BLOCK: no approval of any kind ──────────────────────────────

    def test_no_authorization_at_all_is_blocked(self) -> None:
        # Neither --slack-ts nor --from-on-behalf, no recorded row.
        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7"],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert LivePostApproval.objects.count() == 0

    def test_slack_ts_without_any_approval_phrase_is_blocked(self) -> None:
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": "thumbs up"})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", ts],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert LivePostApproval.objects.count() == 0

    def test_from_on_behalf_falls_through_to_slack_when_no_token(self) -> None:
        # --from-on-behalf with NO recorded approval but a valid --slack-ts
        # falls through to the Slack-DM channel (both flags passed; the
        # on-behalf miss is not fatal when a DM authorization exists).
        ts = self._fresh_ts()
        self._wire_slack_backend(message={"user": "U-OPERATOR", "text": "go ahead"})

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--from-on-behalf", "--slack-ts", ts],
        )

        assert result.exit_code == 0, result.output
        assert LivePostApproval.objects.filter(mr_url="org/repo!7").exists()

    def test_no_user_id_configured_is_refused(self) -> None:
        # A config with no slack_user_id cannot verify any authorization.
        cfg = self.tmp_path / ".teatree.toml"
        cfg.write_text(f'[teatree]\non_behalf_post_mode = "{OnBehalfPostMode.IMMEDIATE.value}"\n', encoding="utf-8")
        self.monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
        self.monkeypatch.setattr("teatree.core.notify.resolve_user_id", lambda: "")

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--from-on-behalf"],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert "user_id" in result.output

    # ── FAIL-OPEN on a broken env never silently AUTHORIZES ─────────

    def test_broken_slack_backend_blocks_not_authorizes(self) -> None:
        # A missing backend must REFUSE (fail closed on the authorization
        # decision) rather than mint a token — fail-open here means "do
        # not grant", the conservative direction for a publish gate.
        self.monkeypatch.setattr(
            "teatree.core.backend_factory.messaging_from_overlay",
            lambda: None,
        )
        ts = self._fresh_ts()

        result = _runner.invoke(
            app,
            ["review", "approve-live-post", "org/repo!7", "--slack-ts", ts],
        )

        assert result.exit_code == 1
        assert "Refused" in result.output
        assert LivePostApproval.objects.count() == 0
