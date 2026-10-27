"""Opt-in worker restart proof that stale addresses are never revived."""

import asyncio
import os
from pathlib import Path

import pytest

from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_mailbox_client import MailboxClient


@pytest.mark.integration
def test_worker_restart_refuses_the_pre_restart_address(tmp_path: Path) -> None:
    if os.environ.get("T3_LIVE_RESTART") != "1":
        pytest.skip("set T3_LIVE_RESTART=1 for the live restart proof")
    with LiveMailboxBroker(runtime_dir=tmp_path) as before:
        old = before.register(room="ticket", harness="claude_sdk", label="old")
    with LiveMailboxBroker(runtime_dir=tmp_path) as after:
        sender = after.register(room="ticket", harness="claude_sdk", label="sender")
        receipt = asyncio.run(MailboxClient(sender.socket_path, sender.token).send(old.address, "nonce", "restart-1"))
    assert (receipt["outcome"], receipt["code"], receipt["queued_in_inbox"]) == (
        "rejected",
        "session_closed",
        False,
    )
