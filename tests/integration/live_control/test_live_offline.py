"""Opt-in live broker proof that an offline recipient is refused explicitly."""

import asyncio
import os
from pathlib import Path

import pytest

from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_mailbox_client import MailboxClient


@pytest.mark.integration
def test_offline_recipient_returns_session_closed(tmp_path: Path) -> None:
    if os.environ.get("T3_LIVE_OFFLINE") != "1":
        pytest.skip("set T3_LIVE_OFFLINE=1 for the live offline proof")
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        sender = broker.register(room="ticket", harness="claude_sdk", label="sender")
        receipt = asyncio.run(
            MailboxClient(sender.socket_path, sender.token).send("offline-address", "nonce", "offline-1")
        )
    assert (receipt["outcome"], receipt["code"], receipt["queued_in_inbox"]) == (
        "rejected",
        "session_closed",
        False,
    )
