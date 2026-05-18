"""The on-behalf pre-gate is enforced at the `_BaseReplier` chokepoint (#960).

`post_in_thread` and `post_comment` post under the user's identity to a
colleague/customer surface, so they are gated. `post_dm` is a bot→user
message and must NEVER be gated. With the gate ON and no recorded
approval, an on-behalf send must NOT record a SENT dispatch — it surfaces
the blocked post (FAILED dispatch + the actionable approve-on-behalf
message); with a recorded approval it proceeds.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import IncomingEvent, OnBehalfApproval, ReplyDispatch
from teatree.core.reply_transport import NoopReplier


def _gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    cfg = tmp_path / ".teatree.toml"
    cfg.write_text(
        f"[teatree]\nask_before_post_on_behalf = {'true' if on else 'false'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)


@pytest.mark.django_db
class TestReplyTransportOnBehalfGate:
    @pytest.fixture(autouse=True)
    def _ctx(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch

    def _event(self, key: str) -> IncomingEvent:
        return IncomingEvent.objects.create(source=IncomingEvent.Source.SLACK, body="x", idempotency_key=key)

    def test_post_in_thread_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        event = self._event("slack:gate-1")
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key="slack:gate-1:reply",
        )
        assert dispatch.status == ReplyDispatch.Status.FAILED
        assert "approve-on-behalf" in dispatch.error_message

    def test_post_comment_blocked_when_gate_on_no_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        event = self._event("slack:gate-2")
        dispatch = NoopReplier().post_comment(
            event=event,
            target_ref="org/repo#7",
            body="lgtm",
            idempotency_key="slack:gate-2:cmt",
        )
        assert dispatch.status == ReplyDispatch.Status.FAILED
        assert "approve-on-behalf" in dispatch.error_message

    def test_post_in_thread_proceeds_with_recorded_approval(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=True)
        event = self._event("slack:gate-3")
        OnBehalfApproval.record(target="C-eng/t1", action="post_in_thread", approver_id="souliane")
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key="slack:gate-3:reply",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT

    def test_post_in_thread_proceeds_when_gate_off(self) -> None:
        _gate(self.tmp_path, self.monkeypatch, on=False)
        event = self._event("slack:gate-4")
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key="slack:gate-4:reply",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT

    def test_post_dm_is_never_gated_even_when_gate_on(self) -> None:
        """bot→user DM is out of scope — must send with the gate ON, no approval."""
        _gate(self.tmp_path, self.monkeypatch, on=True)
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
