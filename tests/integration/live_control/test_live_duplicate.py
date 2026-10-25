"""Opt-in live broker proof that a duplicate key does not enqueue twice."""

import asyncio
import os
from pathlib import Path

import pytest

from teatree.agents.live_delivery import InboxPage, SendReceipt
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_mailbox_client import MailboxClient


@pytest.mark.integration
def test_duplicate_send_key_keeps_one_message_and_reports_late_acceptance(tmp_path: Path) -> None:
    if os.environ.get("T3_LIVE_DUPLICATE") != "1":
        pytest.skip("set T3_LIVE_DUPLICATE=1 for the live duplicate proof")
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        sender = broker.register(room="ticket", harness="claude_sdk", label="sender")
        recipient = broker.register(room="ticket", harness="codex_app_server", label="recipient")
        from_sender = MailboxClient(sender.socket_path, sender.token)
        to_recipient = MailboxClient(recipient.socket_path, recipient.token)

        async def run() -> tuple[SendReceipt, SendReceipt, InboxPage, SendReceipt]:
            first = await from_sender.send(recipient.address, "nonce", "same-key", timeout_seconds=0.01)
            duplicate = await from_sender.send(recipient.address, "nonce", "same-key", timeout_seconds=0.01)
            inbox = await to_recipient.inbox()
            after_read = await from_sender.send(recipient.address, "nonce", "same-key", timeout_seconds=0.01)
            return first, duplicate, inbox, after_read

        first, duplicate, inbox, after_read = asyncio.run(run())
    assert first["id"] == duplicate["id"] == after_read["id"]
    assert [message["text"] for message in inbox["messages"]] == ["nonce"]
    assert after_read["outcome"] == "accepted_inbox_read"
