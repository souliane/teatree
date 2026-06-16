"""The recorded-approval on-behalf gate orchestration (#960/#961).

``require_on_behalf_approval`` is the single chokepoint helper every
on-behalf publish path calls. It exposes the gate's four outcomes
(see :func:`resolve_on_behalf_verdict`):

*   ``IMMEDIATE`` mode → PROCEED verdict → proceed (no approval needed);
*   ``ASK`` or ``DRAFT_OR_ASK`` mode + draft-form action → AUTO_DRAFT
    verdict → record a ``BotPing`` and proceed (no ``OnBehalfApproval``
    consumed, no ``OnBehalfAudit`` written). A draft is colleague-
    invisible, so it is exempt from the gate under EVERY mode;
*   ``ASK`` or ``DRAFT_OR_ASK`` (colleague-VISIBLE action) + recorded
    approval → BLOCK verdict + approval present → consume + audit + proceed;
*   ``ASK`` or ``DRAFT_OR_ASK`` (colleague-VISIBLE action) + no recorded
    approval → raise :class:`OnBehalfPostBlockedError` so the caller
    surfaces the blocked post to the user (never silently drop, never post
    unattended). Default DRAFT_OR_ASK, fail-closed for every colleague-
    visible mutation; drafts are the ungated safe-by-default.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.models import BotPing
from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfAudit
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _write_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(body, encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


def _noop() -> None:
    return None


class TestRecordedOnBehalfGate:
    def test_immediate_mode_proceeds_without_approval(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "immediate"\n')
        require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=_noop)
        assert OnBehalfAudit.objects.count() == 0
        # No DM either — IMMEDIATE is silent.
        assert BotPing.objects.count() == 0

    def test_ask_mode_no_approval_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        with pytest.raises(OnBehalfPostBlockedError) as exc:
            require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=_noop)
        # The message must tell the user exactly how to satisfy the gate (no TTY).
        assert "approve-on-behalf" in str(exc.value)
        assert "org/repo#42" in str(exc.value)

    def test_ask_mode_with_recorded_approval_proceeds_and_audits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")
        require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=_noop)
        assert OnBehalfAudit.objects.filter(target="org/repo#42", action="post_comment").count() == 1
        # Single-use: a second post on the same target+action is blocked again.
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=_noop)

    def test_default_is_fail_closed_for_non_draft_action(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, "[teatree]\n")  # setting unset → DRAFT_OR_ASK
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="t#1", action="post_comment", publish=_noop)

    def test_recorded_approval_scope_is_exact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        OnBehalfApproval.record(target="org/repo#1", action="post_comment", approver_id="souliane")
        # Wrong action — still blocked, the recorded approval does not match.
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#1", action="resolve_discussion", publish=_noop)


class TestPublishCallbackAtomicity:
    """consume + publish + audit are all-or-nothing (#1879).

    A failed publish must roll back the consume so the single-use approval
    is not burned and no ``OnBehalfAudit`` row claims a post that never
    happened. On success the audit is written only after the publish ran.
    """

    def test_block_with_approval_runs_publish_consumes_and_audits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        approval = OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")

        calls: list[str] = []

        def publish() -> str:
            calls.append("posted")
            return "https://example.com/org/repo/comment/1"

        result = require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=publish)

        assert result == "https://example.com/org/repo/comment/1"
        assert calls == ["posted"]
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.filter(target="org/repo#42", action="post_comment").count() == 1

    def test_failed_publish_rolls_back_consume_and_writes_no_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        approval = OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")

        class PostError(RuntimeError):
            pass

        def publish() -> str:
            raise PostError

        with pytest.raises(PostError):
            require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=publish)

        # The approval was NOT burned — a re-attempt can still consume it.
        approval.refresh_from_db()
        assert approval.consumed_at is None, "approval was consumed despite a failed post"
        # No lying audit — nothing claims a post that never happened.
        assert OnBehalfAudit.objects.count() == 0, "audit row written for a post that failed"

    def test_failed_publish_then_retry_succeeds_consuming_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")

        attempts = {"n": 0}

        def flaky_publish() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                msg = "transient"
                raise RuntimeError(msg)
            return "ok"

        with pytest.raises(RuntimeError, match="transient"):
            require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=flaky_publish)
        # The first failure rolled back: the same approval still satisfies the retry.
        result = require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=flaky_publish)
        assert result == "ok"
        assert OnBehalfAudit.objects.filter(target="org/repo#42", action="post_comment").count() == 1
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=False).count() == 1

    def test_immediate_mode_runs_publish_without_consume_or_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "immediate"\n')

        result = require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=lambda: "posted")
        assert result == "posted"
        assert OnBehalfAudit.objects.count() == 0

    def test_block_with_no_approval_never_runs_publish(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')

        calls: list[str] = []

        def publish() -> str:
            calls.append("posted")
            return "posted"

        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=publish)
        assert calls == [], "publish ran despite a BLOCK with no recorded approval"


class TestNonConsumingPeek:
    """``on_behalf_block_message`` reports a verdict without consuming."""

    def test_block_with_no_approval_returns_message_without_consuming(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        msg = on_behalf_block_message("org/repo#42", "post_comment")
        assert "approve-on-behalf" in msg
        assert OnBehalfAudit.objects.count() == 0

    def test_block_with_approval_returns_empty_and_does_not_consume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        approval = OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")
        assert on_behalf_block_message("org/repo#42", "post_comment") == ""
        # The peek must NOT consume — the approval survives for the real post.
        approval.refresh_from_db()
        assert approval.consumed_at is None
        assert OnBehalfAudit.objects.count() == 0

    def test_immediate_mode_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "immediate"\n')
        assert on_behalf_block_message("org/repo#42", "post_comment") == ""

    def test_auto_draft_action_returns_empty_without_dm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')
        assert on_behalf_block_message("org/repo!7", "post_draft_note") == ""
        # A peek never fires the autodraft DM — that is deferred to the atomic publish.
        assert BotPing.objects.count() == 0

    def test_draft_action_peek_returns_empty_under_ask(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A draft is exempt under ASK too — the peek refuses nothing (no approval needed)."""
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')
        assert on_behalf_block_message("org/repo!7", "post_draft_note") == ""
        assert BotPing.objects.count() == 0


class TestAutoDraftVerdict:
    """A draft-form action auto-drafts (records a DM and proceeds) under EVERY blocking mode."""

    def test_post_draft_note_under_draft_or_ask_records_bot_ping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)

        # (i) no OnBehalfApproval was consumed (none existed, none created).
        assert OnBehalfApproval.objects.count() == 0
        # (ii) a BotPing was recorded under the canonical idempotency key.
        ping = BotPing.objects.get(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note")
        assert ping.kind == BotPing.Kind.INFO
        # (iii) no OnBehalfAudit was written — AUTO_DRAFT doesn't consume an approval.
        assert OnBehalfAudit.objects.count() == 0

    def test_post_draft_note_under_ask_records_bot_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """ANTI-VACUITY: even under strict ASK, a draft auto-drafts with no approval.

        Pre-fix this raised :class:`OnBehalfPostBlockedError` (the bug).
        The fix exempts drafts from the gate under ASK too.
        """
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "ask"\n')

        # Must NOT raise — a draft is exempt under ASK.
        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)

        assert OnBehalfApproval.objects.count() == 0
        ping = BotPing.objects.get(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note")
        assert ping.kind == BotPing.Kind.INFO
        assert OnBehalfAudit.objects.count() == 0

    def test_double_call_is_idempotent_on_bot_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)
        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)

        assert BotPing.objects.filter(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note").count() == 1

    def test_non_draft_action_under_draft_or_ask_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``draft_or_ask`` only auto-drafts ``post_draft_note`` — other actions BLOCK."""
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo!7", action="post_comment", publish=_noop)
        # No spurious BotPing for blocked actions.
        assert BotPing.objects.count() == 0


class TestAutoDraftDmFailureNeverRaises:
    """``notify_user`` failures must not bubble up — drafts must publish either way."""

    def test_notify_user_returning_false_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_cfg(tmp_path, monkeypatch, '[teatree]\non_behalf_post_mode = "draft_or_ask"\n')

        with patch("teatree.notify.notify_user", return_value=False):
            # Must return cleanly even when the DM degraded to a no-op.
            require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)
