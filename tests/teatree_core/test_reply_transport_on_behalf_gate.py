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

import pytest
from django.test import TestCase

from teatree.config import OnBehalfPostMode
from teatree.core.models import IncomingEvent, OnBehalfApproval, ReplyDispatch
from teatree.core.reply_transport import NoopReplier


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: OnBehalfPostMode) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f'[teatree]\non_behalf_post_mode = "{mode.value}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


_BLOCKING_MODES = [OnBehalfPostMode.ASK, OnBehalfPostMode.DRAFT_OR_ASK]


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


# Standalone django_db functions must be grouped in a TestCase (#98); this
# minimal companion documents the contract via the TestCase base too.
class TestBotToUserNotGatedTestCase(TestCase):
    def test_post_dm_records_sent(self) -> None:
        event = IncomingEvent.objects.create(source=IncomingEvent.Source.SLACK, body="x", idempotency_key="slack:tc-dm")
        dispatch = NoopReplier().post_dm(event=event, actor="souliane", body="hi", idempotency_key="slack:tc-dm:dm")
        assert dispatch.status == ReplyDispatch.Status.SENT
