"""The recorded-approval on-behalf gate orchestration (#960/#961).

``require_on_behalf_approval`` is the single chokepoint helper every
on-behalf publish path calls. It exposes the gate's four outcomes
(see :func:`resolve_on_behalf_verdict`):

*   a permitting posture → PROCEED verdict → proceed (no approval needed);
*   a forbidding posture + draft-form action → AUTO_DRAFT verdict → record a
    ``BotPing`` and proceed (no ``OnBehalfApproval`` consumed, no
    ``OnBehalfAudit`` written). A draft is colleague-invisible, so it is
    exempt from the gate under EVERY posture;
*   a forbidding posture (colleague-VISIBLE action) + recorded approval →
    BLOCK verdict + approval present → consume + audit + proceed;
*   a forbidding posture (colleague-VISIBLE action) + no recorded approval →
    raise :class:`OnBehalfPostBlockedError` so the caller surfaces the
    blocked post to the user (never silently drop, never post unattended).
    A forbidding posture is fail-closed for every colleague-visible
    mutation; drafts are the ungated safe-by-default.

Which posture the shipped defaults resolve to is a separate question from what
each posture DOES, and this suite owns the second — EVERY case here pins the
posture it exercises, with no exception. It cannot stand in for the first: a
seeded fresh install resolves ``present``, which PERMITS, so nothing in this file
observes what a fresh box does. That is
``tests/teatree_loops/test_preset_seed.py::TestTheSeededPosturesDecideTheOwnersVoice``,
and the unseeded fail-closed half is ``tests/config/test_autonomy.py``.

The conformance gate over retired vocabulary
(``tests/conformance/test_retired_egress_control_is_not_instructed.py``) does not
reach a claim like the one this paragraph replaced: it matches the retired dial's
spellings, and a docstring can be false about the posture without naming one.
"""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.core.mode_resolution import set_mode_override
from teatree.core.models import BotPing, Mode, ModeOverride
from teatree.core.models.loop_preset import Egress
from teatree.core.models.on_behalf_approval import OnBehalfApproval, OnBehalfAudit
from teatree.core.on_behalf_gate_recorded import (
    OnBehalfPostBlockedError,
    on_behalf_block_message,
    require_on_behalf_approval,
)
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture, seed_permitting_posture

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _set_posture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, forbidding: bool) -> None:
    """Pin the posture the chokepoint reads.

    ``forbidding=None`` is not offered: a case that leans on the fail-closed default
    states nothing about which posture it exercises, and the default is what a later
    change to the shipped rows would move underneath it.
    """
    if forbidding:
        seed_forbidding_posture()
    else:
        seed_permitting_posture()


def _permit_egress() -> None:
    """Pin a posture that allows the owner's voice out — how the gate is opened now.

    The retired per-post dial is not what these cases are about; they own what the
    recorded gate DOES once a verdict is reached. Seeding a real ``Mode`` + override
    (rather than stubbing the resolver) keeps them honest about which layer decides.
    """
    Mode.objects.update_or_create(name="present", defaults={"entries": {}, "egress": "allow"})
    ModeOverride.objects.set_override("present", reason="permitting posture for this test")


def _noop() -> None:
    return None


def _stage_forbidding_override(reason: str) -> None:
    """A real ``afk`` row selected by a manual override — the posture the setting cannot open."""
    Mode.objects.update_or_create(name="afk", defaults={"entries": {}, "egress": Egress.FORBID})
    set_mode_override("afk", reason=reason)


class TestRecordedOnBehalfGate:
    def test_a_permitting_posture_proceeds_without_approval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _permit_egress()
        require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=_noop)
        assert OnBehalfAudit.objects.count() == 0
        # No DM either — a permitting posture is silent.
        assert BotPing.objects.count() == 0

    def test_ask_mode_no_approval_blocks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)
        with pytest.raises(OnBehalfPostBlockedError) as exc:
            require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=_noop)
        # The message must tell the user exactly how to satisfy the gate (no TTY).
        assert "approve-on-behalf" in str(exc.value)
        assert "org/repo#42" in str(exc.value)

    def test_ask_mode_with_recorded_approval_proceeds_and_audits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)
        OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")
        require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=_noop)
        assert OnBehalfAudit.objects.filter(target="org/repo#42", action="post_comment").count() == 1
        # Single-use: a second post on the same target+action is blocked again.
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=_noop)

    def test_a_forbidding_posture_blocks_a_non_draft_action(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="t#1", action="post_comment", publish=_noop)

    def test_a_forbidding_posture_blocks_and_writes_no_audit_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #3895: no autonomy tier reaches the posture, so a forbidding one refuses
        # whatever the tier resolves to.
        seed_forbidding_posture()
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="t#1", action="post_comment", publish=_noop)
        assert OnBehalfAudit.objects.count() == 0

    def test_recorded_approval_scope_is_exact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)
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
        _set_posture(tmp_path, monkeypatch, forbidding=True)
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
        _set_posture(tmp_path, monkeypatch, forbidding=True)
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
        _set_posture(tmp_path, monkeypatch, forbidding=True)
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

    def test_a_permitting_posture_runs_publish_without_consume_or_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _permit_egress()

        result = require_on_behalf_approval(target="org/repo#42", action="post_comment", publish=lambda: "posted")
        assert result == "posted"
        assert OnBehalfAudit.objects.count() == 0

    def test_block_with_no_approval_never_runs_publish(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)

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
        _set_posture(tmp_path, monkeypatch, forbidding=True)
        msg = on_behalf_block_message("org/repo#42", "post_comment")
        assert "approve-on-behalf" in msg
        assert OnBehalfAudit.objects.count() == 0

    def test_block_with_approval_returns_empty_and_does_not_consume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)
        approval = OnBehalfApproval.record(target="org/repo#42", action="post_comment", approver_id="souliane")
        assert on_behalf_block_message("org/repo#42", "post_comment") == ""
        # The peek must NOT consume — the approval survives for the real post.
        approval.refresh_from_db()
        assert approval.consumed_at is None
        assert OnBehalfAudit.objects.count() == 0

    def test_a_permitting_posture_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _permit_egress()
        assert on_behalf_block_message("org/repo#42", "post_comment") == ""

    def test_auto_draft_action_returns_empty_without_dm(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)
        assert on_behalf_block_message("org/repo!7", "post_draft_note") == ""
        # A peek never fires the autodraft DM — that is deferred to the atomic publish.
        assert BotPing.objects.count() == 0


class TestAutoDraftVerdict:
    """A draft-form action auto-drafts (records a DM and proceeds) under a forbidding posture."""

    def test_post_draft_note_under_a_forbidding_posture_records_bot_ping(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ANTI-VACUITY: a draft auto-drafts with no approval even where the posture refuses.

        Pre-fix this raised :class:`OnBehalfPostBlockedError` (the bug).
        """
        _set_posture(tmp_path, monkeypatch, forbidding=True)

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)

        # (i) no OnBehalfApproval was consumed (none existed, none created).
        assert OnBehalfApproval.objects.count() == 0
        # (ii) a BotPing was recorded under the canonical idempotency key.
        ping = BotPing.objects.get(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note")
        assert ping.kind == BotPing.Kind.INFO
        # (iii) no OnBehalfAudit was written — AUTO_DRAFT doesn't consume an approval.
        assert OnBehalfAudit.objects.count() == 0

    def test_double_call_is_idempotent_on_bot_ping(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)

        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)
        require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)

        assert BotPing.objects.filter(idempotency_key="on_behalf_autodraft:org/repo!7:post_draft_note").count() == 1

    def test_non_draft_action_under_a_forbidding_posture_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The AUTO_DRAFT carve-out is ``post_draft_note`` alone — other actions BLOCK."""
        _set_posture(tmp_path, monkeypatch, forbidding=True)
        with pytest.raises(OnBehalfPostBlockedError):
            require_on_behalf_approval(target="org/repo!7", action="post_comment", publish=_noop)
        # No spurious BotPing for blocked actions.
        assert BotPing.objects.count() == 0


class TestAutoDraftDmFailureNeverRaises:
    """``notify_user`` failures must not bubble up — drafts must publish either way."""

    def test_notify_user_returning_false_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_posture(tmp_path, monkeypatch, forbidding=True)

        with patch("teatree.core.notify.notify_user", return_value=False):
            # Must return cleanly even when the DM degraded to a no-op.
            require_on_behalf_approval(target="org/repo!7", action="post_draft_note", publish=_noop)


class TestARefusalNamesItsCause:
    """A final refusal names the posture and its layer; a BLOCK an approval then satisfies logs nothing."""

    def test_a_posture_refusal_names_the_posture_and_the_layer_that_set_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_forbidding_override("owner afk; box healthy after restart")
        with pytest.raises(OnBehalfPostBlockedError) as caught:
            require_on_behalf_approval(target="C_TEAM", action="cli_notify_post", publish=_noop)
        message = str(caught.value)
        assert "the active posture 'afk'" in message
        assert "manual override — owner afk; box healthy after restart" in message
        assert "loop preset use present" in message
        assert "on_behalf_post_mode" not in message

    def test_the_peek_names_the_same_cause(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stage_forbidding_override("owner afk")
        assert "the active posture 'afk'" in on_behalf_block_message("C_TEAM", "cli_notify_post")

    def test_a_block_an_approval_satisfies_publishes_and_logs_no_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _stage_forbidding_override("owner afk")
        OnBehalfApproval.record(target="C_TEAM", action="cli_notify_post", approver_id="souliane")
        with caplog.at_level(logging.ERROR, logger="teatree.core.on_behalf_gate_recorded"):
            posted = require_on_behalf_approval(target="C_TEAM", action="cli_notify_post", publish=lambda: "posted")
        assert posted == "posted"
        assert [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR] == []
        assert OnBehalfAudit.objects.filter(target="C_TEAM", action="cli_notify_post").count() == 1
