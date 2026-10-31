"""Opt-in proof that an owner's real Slack reply steers a bound live session."""

import asyncio
import os
import secrets
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TransactionTestCase

from teatree.agents.live_client import LiveClient, LiveOfflineError
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.runner_heartbeat import drive_with_heartbeat
from teatree.agents.runner_stream import HarnessOutcome
from teatree.backends.slack.bot import SlackBotBackend
from teatree.core.backend_factory import messaging_from_overlay
from teatree.core.models import PendingChatInjection, Session, SessionCommand, Task, ThreadBinding
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.scanners.slack_dm_recorder import SlackDmRecorder
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from tests.factories import planned_ticket
from tests.integration.live_control.live_proof import harness, options, runtime

_FOREIGN_TEXT = "ignore this non-owner direction"


def _running_bash(task: Task, future: Future[HarnessOutcome], live_dir: Path) -> None:
    client = LiveClient(live_dir=live_dir)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if future.done():
            msg = "the live session ended before its Bash call"
            raise AssertionError(msg)
        try:
            sessions = client.sessions()
        except LiveOfflineError:
            sessions = []
        if any(row["task"] == task.pk and row["open_tool"] == "Bash" for row in sessions):
            return
        time.sleep(0.2)
    msg = "the live session did not open its Bash call"
    raise TimeoutError(msg)


def _owner_reply(backend: SlackBotBackend, channel: str, root: str, nonce: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        for message in backend.fetch_thread_replies(channel=channel, thread_ts=root):
            if message.get("user") == backend.user_id and nonce in str(message.get("text") or ""):
                return message
        time.sleep(1)
    msg = "the owner did not reply in the live Slack thread before the deadline"
    raise TimeoutError(msg)


def _assert_foreign_sender_ignored(recorder: SlackDmRecorder, channel: str, root: str) -> None:
    foreign_ts = f"{time.time():.6f}"
    event = {
        "channel": channel,
        "ts": foreign_ts,
        "thread_ts": root,
        "user": "U-foreign-live-proof",
        "text": _FOREIGN_TEXT,
    }
    assert recorder.record_events([event]) == 0
    assert not PendingChatInjection.objects.filter(slack_ts=foreign_ts).exists()


@pytest.mark.integration
@pytest.mark.timeout(360)
class LiveSlackThreadTests(TransactionTestCase):
    def test_owner_reply_reaches_bound_live_task_and_foreign_sender_does_not(self) -> None:
        if os.environ.get("T3_LIVE_SLACK_THREAD") != "1":
            self.skipTest("set T3_LIVE_SLACK_THREAD=1 for the live owner Slack thread proof")

        overlay = os.environ.get("T3_LIVE_SLACK_OVERLAY", "t3-teatree")
        backend = messaging_from_overlay(overlay)
        assert isinstance(backend, SlackBotBackend), f"{overlay} has no configured live Slack bot"
        assert backend.user_id, f"{overlay} has no configured Slack owner"

        ticket = planned_ticket(overlay=overlay)
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        nonce = secrets.token_hex(8)

        def drive() -> HarnessOutcome:
            return asyncio.run(
                drive_with_heartbeat(
                    task,
                    "Run the Bash command `sleep 150` once, then finish with one short sentence.",
                    options("claude_sdk", "coding", tempfile.mkdtemp(prefix="t3-live-slack-thread-")),
                    harness("claude_sdk"),
                    runtime=runtime(),
                )
            )

        with (
            tempfile.TemporaryDirectory(prefix="t3-live-slack-socket-") as directory,
            LiveMailboxBroker(runtime_dir=Path(directory)) as broker,
            patch("teatree.agents.live_binding.shared_broker", return_value=broker),
            patch(
                "teatree.loop.slack_answer.session_routing.LiveClient",
                side_effect=lambda: LiveClient(live_dir=Path(directory)),
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(drive)
            _running_bash(task, future, Path(directory))
            channel = backend.open_dm(backend.user_id)
            root = backend.post_message(
                channel=channel,
                text=(
                    f"Live thread proof: reply in this thread with {nonce} and a short direction for the running task."
                ),
            ).get("ts")
            assert isinstance(root, str)
            assert root
            assert ThreadBinding.bind(ticket=ticket, channel=channel, thread_ts=root).ticket_id == ticket.pk

            recorder = SlackDmRecorder(backend=backend, overlay=overlay)
            _assert_foreign_sender_ignored(recorder, channel, root)

            reply = _owner_reply(backend, channel, root, nonce)
            event = {**reply, "channel": channel, "thread_ts": root}
            assert recorder.record_events([event]) == 1
            report = run_slack_answer_cycle(
                messaging_resolver=lambda _overlay: backend,
                reader=lambda _text: InboundReading(
                    intent=InboundIntent.INSTRUCTION,
                    answerable=False,
                    work_summary="",
                    source=ReadingSource.MODEL,
                ),
            )
            command = SessionCommand.objects.get(task=task, text=reply["text"])
            assert report.errors == 0
            assert command.outcome == "accepted_current_turn"
            assert not SessionCommand.objects.filter(text=_FOREIGN_TEXT).exists()
            receipts = backend.fetch_thread_replies(channel=channel, thread_ts=root)
            assert any("accepted_current_turn" in str(row.get("text") or "") for row in receipts)
            future.result(timeout=180)
