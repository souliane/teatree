"""The tri-state on-behalf pre-gate is enforced at the ``_BaseReplier`` chokepoint (#960).

``post_in_thread`` and ``post_comment`` post under the user's identity
to a colleague/customer surface, so they are gated. ``post_dm`` is a
bot→user message and must NEVER be gated. Under a blocking mode
(``ASK`` or ``DRAFT_OR_ASK``) with no recorded approval, an on-behalf
send must NOT record a SENT dispatch — it surfaces the blocked post
(FAILED dispatch + the actionable approve-on-behalf message); with a
recorded approval it proceeds. Under ``IMMEDIATE`` every send
publishes.

Reply-transport actions (``post_in_thread``, ``post_comment``) are NOT
draft-form actions: they BLOCK identically under ASK and DRAFT_OR_ASK.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from teatree.config import OnBehalfPostMode
from teatree.core.models import BotPing, IncomingEvent, OnBehalfApproval, OnBehalfAudit, ReplyDispatch
from teatree.core.reply_transport import NoopReplier, ReplySpec, _BaseReplier


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: OnBehalfPostMode) -> None:
    # ``on_behalf_post_mode`` is DB-home (#1775) so a TOML value for it is
    # ignored on read — stage it via the ``T3_*`` env tier, which wins for a
    # DB-home key and needs no DB.
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text("[teatree]\n", encoding="utf-8")
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", mode.value)


_BLOCKING_MODES = [OnBehalfPostMode.ASK, OnBehalfPostMode.DRAFT_OR_ASK]


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestReplyTransportOnBehalfGate:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def _event(self, key: str) -> IncomingEvent:
        return IncomingEvent.objects.create(source=IncomingEvent.Source.SLACK, body="x", idempotency_key=key)

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_post_in_thread_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        event = self._event(f"slack:gate-1:{mode.value}")
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key=f"slack:gate-1:reply:{mode.value}",
        )
        assert dispatch.status == ReplyDispatch.Status.FAILED
        assert "approve-on-behalf" in dispatch.error_message

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_post_comment_blocked_when_no_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        event = self._event(f"slack:gate-2:{mode.value}")
        dispatch = NoopReplier().post_comment(
            event=event,
            target_ref="org/repo#7",
            body="lgtm",
            idempotency_key=f"slack:gate-2:cmt:{mode.value}",
        )
        assert dispatch.status == ReplyDispatch.Status.FAILED
        assert "approve-on-behalf" in dispatch.error_message

    @pytest.mark.parametrize("mode", _BLOCKING_MODES)
    def test_post_in_thread_proceeds_with_recorded_approval(self, mode: OnBehalfPostMode) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=mode)
        event = self._event(f"slack:gate-3:{mode.value}")
        OnBehalfApproval.record(target="C-eng/t1", action="post_in_thread", approver_id="souliane")
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key=f"slack:gate-3:reply:{mode.value}",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT

    def test_post_in_thread_proceeds_under_immediate(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.IMMEDIATE)
        event = self._event("slack:gate-4")
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key="slack:gate-4:reply",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT

    def test_post_dm_is_never_gated_even_when_blocking(self) -> None:
        """bot→user DM is out of scope — must send with the gate ON, no approval."""
        _gate(self.tmp_path, self.monkeypatch, mode=OnBehalfPostMode.ASK)
        event = self._event("slack:gate-5")
        dispatch = NoopReplier().post_dm(
            event=event,
            actor="souliane",
            body="your loop needs attention",
            idempotency_key="slack:gate-5:dm",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT


def _notify_backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D-OPERATOR"
    backend.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
    backend.get_permalink.return_value = "https://slack.example/archives/D-OPERATOR/p1"
    return backend


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestReplyTransportAfterReceiptDm:
    """#949: a SENT on-behalf reply fires one after-receipt DM; post_dm does not."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``slack_user_id`` is a RAW key (TOML-home); ``on_behalf_post_mode``
        # is DB-home (#1775) so a TOML value for it is ignored on read — stage
        # it via the ``T3_*`` env tier.
        cfg = tmp_path / ".teatree.toml"
        cfg.write_text(
            '[teatree]\nslack_user_id = "U-OPERATOR"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
        monkeypatch.setenv("T3_ON_BEHALF_POST_MODE", "immediate")
        self.monkeypatch = monkeypatch

    def _event(self, key: str) -> IncomingEvent:
        return IncomingEvent.objects.create(source=IncomingEvent.Source.SLACK, body="x", idempotency_key=key)

    def test_on_behalf_reply_emits_after_receipt_dm_on_sent(self) -> None:
        backend = _notify_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)
        event = self._event("slack:ar-1")
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello team",
            idempotency_key="slack:ar-1:reply",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT
        ping = BotPing.objects.get(idempotency_key="on_behalf_post:C-eng/t1:post_in_thread")
        assert ping.status == BotPing.Status.SENT
        assert "hello team" in ping.text

    def test_post_dm_action_does_not_emit_after_receipt_dm(self) -> None:
        """Scope guard: bot→user DM is internal — never an after-receipt post."""
        backend = _notify_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)
        event = self._event("slack:ar-2")
        dispatch = NoopReplier().post_dm(
            event=event,
            actor="souliane",
            body="your loop needs attention",
            idempotency_key="slack:ar-2:dm",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT
        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()

    def test_redeliver_emits_after_receipt_dm_for_on_behalf_action(self) -> None:
        """The retry-sweep path (`redeliver`) also fires the after-receipt DM."""
        backend = _notify_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)
        event = self._event("slack:ar-3")
        dispatch = ReplyDispatch.objects.create(
            event=event,
            target_ref="C-eng/t9",
            action_name="post_comment",
            status=ReplyDispatch.Status.FAILED,
            body="retry body",
            idempotency_key="slack:ar-3:reply",
        )
        NoopReplier().redeliver(dispatch)

        ping = BotPing.objects.get(idempotency_key="on_behalf_post:C-eng/t9:post_comment")
        assert ping.status == BotPing.Status.SENT
        assert "retry body" in ping.text

    def test_redeliver_post_dm_does_not_emit_after_receipt_dm(self) -> None:
        """Scope guard on the retry path: a redelivered post_dm stays internal."""
        backend = _notify_backend()
        self.monkeypatch.setattr("teatree.core.notify.messaging_from_overlay", lambda: backend)
        event = self._event("slack:ar-4")
        dispatch = ReplyDispatch.objects.create(
            event=event,
            target_ref="souliane",
            action_name="post_dm",
            status=ReplyDispatch.Status.FAILED,
            body="bot to user",
            idempotency_key="slack:ar-4:dm",
        )
        NoopReplier().redeliver(dispatch)

        assert not BotPing.objects.filter(idempotency_key__startswith="on_behalf_post:").exists()


class _FlakyReplier(_BaseReplier):
    """A replier whose ``_deliver`` fails until ``fail_first`` deliveries pass."""

    def __init__(self, *, fail_count: int) -> None:
        self._remaining_failures = fail_count
        self.delivery_attempts = 0

    def _deliver(self, spec: ReplySpec) -> str:
        self.delivery_attempts += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            msg = "transient backend failure"
            raise RuntimeError(msg)
        return "https://example.com/posted"


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db
class TestRedeliverReusesReservation:
    """#1879: a failed redeliver rolls back the consume — the approval is reused, not burned per retry."""

    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _gate(tmp_path, monkeypatch, mode=OnBehalfPostMode.ASK)
        self.monkeypatch = monkeypatch

    def _event(self, key: str) -> IncomingEvent:
        return IncomingEvent.objects.create(source=IncomingEvent.Source.SLACK, body="x", idempotency_key=key)

    def _failed_dispatch(self, event: IncomingEvent, key: str) -> ReplyDispatch:
        return ReplyDispatch.objects.create(
            event=event,
            target_ref="org/repo#7",
            action_name="post_comment",
            status=ReplyDispatch.Status.FAILED,
            body="retry body",
            idempotency_key=key,
        )

    def test_failed_redeliver_does_not_burn_the_approval(self) -> None:
        approval = OnBehalfApproval.record(target="org/repo#7", action="post_comment", approver_id="souliane")
        event = self._event("slack:rr-1")
        dispatch = self._failed_dispatch(event, "slack:rr-1:reply")

        with pytest.raises(RuntimeError, match="transient"):
            _FlakyReplier(fail_count=1).redeliver(dispatch)

        # The failed redeliver rolled back the consume — the approval survives.
        approval.refresh_from_db()
        assert approval.consumed_at is None, "a failed redeliver burned the approval"
        assert OnBehalfAudit.objects.count() == 0, "a failed redeliver wrote a lying audit"

    def test_n_redelivers_consume_exactly_one_approval(self) -> None:
        # One recorded approval must satisfy a flaky redeliver that fails twice
        # then succeeds — N retries must NOT burn N approvals.
        OnBehalfApproval.record(target="org/repo#7", action="post_comment", approver_id="souliane")
        event = self._event("slack:rr-2")
        dispatch = self._failed_dispatch(event, "slack:rr-2:reply")
        replier = _FlakyReplier(fail_count=2)

        for _ in range(2):
            with pytest.raises(RuntimeError, match="transient"):
                replier.redeliver(dispatch)
        # The third redeliver succeeds against the SAME single recorded approval.
        replier.redeliver(dispatch)

        assert replier.delivery_attempts == 3
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=False).count() == 1
        assert OnBehalfAudit.objects.filter(target="org/repo#7", action="post_comment").count() == 1


# Standalone django_db functions must be grouped in a TestCase (#98); this
# minimal companion documents the contract via the TestCase base too.
class TestBotToUserNotGatedTestCase(TestCase):
    def test_post_dm_records_sent(self) -> None:
        event = IncomingEvent.objects.create(source=IncomingEvent.Source.SLACK, body="x", idempotency_key="slack:tc-dm")
        dispatch = NoopReplier().post_dm(event=event, actor="souliane", body="hi", idempotency_key="slack:tc-dm:dm")
        assert dispatch.status == ReplyDispatch.Status.SENT
