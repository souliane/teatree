"""The recorded-approval on-behalf gate orchestration (#960/#961).

``require_on_behalf_approval`` is the single chokepoint helper every
on-behalf publish path calls. It exposes the gate's four outcomes
(see :func:`resolve_on_behalf_verdict`):

*   ``IMMEDIATE`` mode → PROCEED verdict → proceed (no approval needed);
*   ``DRAFT_OR_ASK`` mode + draft-form action → AUTO_DRAFT verdict →
    record a ``BotPing`` and proceed (no ``OnBehalfApproval`` consumed,
    no ``OnBehalfAudit`` written);
*   ``ASK`` or ``DRAFT_OR_ASK`` (non-draft action) + recorded approval
    → BLOCK verdict + approval present → consume + audit + proceed;
*   ``ASK`` or ``DRAFT_OR_ASK`` (non-draft action) + no recorded approval
    → raise :class:`OnBehalfPostBlockedError` so the caller surfaces the
    blocked post to the user (never silently drop, never post
    unattended). Default DRAFT_OR_ASK, fail-closed for every non-draft
    colleague-visible mutation.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.models import BotPing
from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfAudit
from teatree.core.on_behalf_gate_recorded import OnBehalfPostBlockedError, require_on_behalf_approval

pytestmark = pytest.mark.django_db


def _write_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


class TestRecordedOnBehalfGate:
    def test_immediate_mode_proceeds_without_approval(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "immediate"\n')
        require_on_behalf_approval(target="org/repo#42", action="post_comment")
        assert OnBehalfAudit.objects.count() == 0
        # No DM either — IMMEDIATE is silent.
        assert BotPing.objects.count() == 0

    def test_ask_mode_no_approval_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        with pytest.raises(OnBehalfPostBlockedError) as exc:
            require_on_behalf_approval(target="org/repo#42", action="post_comment")
        # The message must tell the user exactly how to satisfy the gate (no TTY).
        assert "approve-on-behalf" in str(exc.value)
        assert "org/repo#42" in str(exc.value)

    def test_ask_mode_with_recorded_approval_proceeds_and_audits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")
        require_on_behalf_approval(target="org/repo#42", action="post_comment")
        assert OnBehalfAudit.objects.filter(target="org/repo#42", action="post_comment").count() == 1
        # Single-use: a second post on the same target+action is blocked again.
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#42", action="post_comment")

    def test_default_is_fail_closed_for_non_draft_action(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, "[teatree]\n")  # setting unset → DRAFT_OR_ASK
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="t#1", action="post_comment")

    def test_recorded_approval_scope_is_exact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        OnBehalfApproval.record(target="org/repo#1", action="post_comment", approver_id="souliane")
        # Wrong action — still blocked, the recorded approval does not match.
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#1", action="resolve_discussion")


class TestAutoDraftVerdict:
    """Under DRAFT_OR_ASK + draft-form action, the helper records a DM and returns."""

    def test_post_draft_note_under_draft_or_ask_records_bot_ping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note")

        # (i) no OnBehalfApproval was consumed (none existed, none created).
        assert OnBehalfApproval.objects.count() == 0
        # (ii) a BotPing was recorded under the canonical idempotency key.
        ping = BotPing.objects.get(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note")
        assert ping.kind == BotPing.Kind.INFO
        # (iii) no OnBehalfAudit was written — AUTO_DRAFT doesn't consume an approval.
        assert OnBehalfAudit.objects.count() == 0

    def test_double_call_is_idempotent_on_bot_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note")
        require_on_behalf_approval(target="org/repo!7", action="post_draft_note")

        assert BotPing.objects.filter(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note").count() == 1

    def test_non_draft_action_under_draft_or_ask_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``draft_or_ask`` only auto-drafts ``post_draft_note`` — other actions BLOCK."""
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo!7", action="post_comment")
        # No spurious BotPing for blocked actions.
        assert BotPing.objects.count() == 0


class TestAutoDraftDmFailureNeverRaises:
    """``notify_user`` failures must not bubble up — drafts must publish either way."""

    def test_notify_user_returning_false_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')

        with patch("teatree.notify.notify_user", return_value=False):
            # Must return cleanly even when the DM degraded to a no-op.
            require_on_behalf_approval(target="org/repo!7", action="post_draft_note")
