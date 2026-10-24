"""Shared real-provider setup for opt-in mailbox proof runs."""

import asyncio
import os
import secrets
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents._runner_options import _wire_teatree_mcp_server
from teatree.agents.codex_app_server import CodexAppServerHarness
from teatree.agents.compaction_guard import CompactionGuard, GuardedHarness
from teatree.agents.harness import ClaudeSdkHarness
from teatree.agents.live_delivery import PeerDelivery, SendReceipt
from teatree.agents.live_mailbox import _Participant, shared_broker
from teatree.agents.model_tiering import tier_models
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import Session, Task
from teatree.credential_config import resolve_api_key_credential, resolve_subscription_credential
from tests.factories import planned_ticket


def runtime() -> HeartbeatRuntime:
    return HeartbeatRuntime(
        watchdog=LoopWatchdog(max_runtime_seconds=180, max_turns=0, max_cost_usd=0.0),
        heartbeat_interval=10,
        sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
        renew_lease=lambda _task: None,
        drain_reason=lambda: "",
    )


def options(kind: str, phase: str, cwd: str) -> ClaudeAgentOptions:
    if kind == "claude_sdk":
        if os.environ.get("T3_LIVE_NONCE_CREDENTIAL") == "api_key":
            credential_env, credential = "ANTHROPIC_API_KEY", resolve_api_key_credential().resolve()
        else:
            credential_env, credential = "CLAUDE_CODE_OAUTH_TOKEN", resolve_subscription_credential().resolve()
        configured = ClaudeAgentOptions(
            model=tier_models()["cheap"],
            permission_mode="bypassPermissions",
            cwd=cwd,
            setting_sources=[],
            max_turns=12,
            env={credential_env: credential},
        )
    else:
        configured = ClaudeAgentOptions(
            model="gpt-5.6-sol", permission_mode="bypassPermissions", cwd=cwd, setting_sources=[], max_turns=12
        )
    if kind == "claude_sdk":
        _wire_teatree_mcp_server(configured, phase=phase)
    return configured


def harness(kind: str) -> GuardedHarness:
    if kind == "claude_sdk":
        return GuardedHarness(ClaudeSdkHarness(), CompactionGuard(), kind)
    return GuardedHarness(
        CodexAppServerHarness(refusal=None, code_home=Path(tempfile.mkdtemp(prefix="t3-live-codex-auth-"))),
        CompactionGuard(),
        kind,
    )


def wait_for_peer(task_pk: int, recipient_future: Future[HarnessOutcome], *, timeout: float = 90) -> str:
    broker = shared_broker()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for peer in broker.identities():
            if peer["label"].endswith(f"task {task_pk}"):
                return peer["address"]
        if recipient_future.done():
            outcome = recipient_future.result()
            msg = f"task {task_pk} completed before joining the mailbox: {outcome.agent_text}"
            raise AssertionError(msg)
        time.sleep(0.1)
    msg = f"task {task_pk} did not join the mailbox"
    raise TimeoutError(msg)


def run_pair(
    sender_kind: str,
    recipient_kind: str,
    *,
    phase: str = "coding",
    sender_phase: str = "coding",
    round_trip: bool = False,
) -> dict[str, Any]:
    ticket = planned_ticket()
    recipient_task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)
    sender_task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=sender_phase)
    nonce = secrets.token_hex(12)
    reply_nonce = secrets.token_hex(12)
    cwd = tempfile.mkdtemp(prefix="t3-live-pair-")
    recipient_options = options(recipient_kind, phase, cwd)
    sender_options = options(sender_kind, sender_phase, cwd)
    seen: dict[str, Any] = {}
    broker = shared_broker()
    original_accepted = broker._accepted

    async def record(delivery: PeerDelivery, recipient: _Participant | None, wait: float) -> SendReceipt:
        receipt = await original_accepted(delivery, recipient, wait)
        if delivery.text == nonce:
            seen["receipt"] = receipt
        if delivery.text == reply_nonce:
            seen["reply_receipt"] = receipt
        return receipt

    def receive() -> HarnessOutcome:
        return asyncio.run(
            drive_with_heartbeat(
                recipient_task,
                "Use the TeaTree mailbox wait tool to read one peer message. "
                + (
                    f"Send the sender a reply containing exactly '{reply_nonce}' with the mailbox send tool. "
                    if round_trip
                    else ""
                )
                + "Include the message you received in your final answer.",
                recipient_options,
                harness(recipient_kind),
                runtime=runtime(),
            )
        )

    with patch.object(broker, "_accepted", record), ThreadPoolExecutor(max_workers=1) as pool:
        recipient_future = pool.submit(receive)
        address = wait_for_peer(recipient_task.pk, recipient_future)
        seen["sender"] = asyncio.run(
            drive_with_heartbeat(
                sender_task,
                "Use TeaTree mailbox peers to find the live peer labelled "
                f"'{phase} task {recipient_task.pk}' (address {address}), then send it exactly '{nonce}' "
                "with a fresh key using the mailbox send tool. "
                + (
                    "Then use the mailbox wait tool to read its reply and include that reply in your final answer."
                    if round_trip
                    else "Report the complete receipt."
                ),
                sender_options,
                harness(sender_kind),
                runtime=runtime(),
            )
        )
        seen["recipient"] = recipient_future.result(timeout=150)
    recipient = seen["recipient"]
    sender = seen.get("sender")
    assert isinstance(recipient, HarnessOutcome), seen
    assert isinstance(sender, HarnessOutcome), seen
    assert nonce in recipient.agent_text, seen
    assert seen.get("receipt", {}).get("outcome") in {"accepted_current_turn", "accepted_inbox_read"}, seen
    if round_trip:
        assert reply_nonce in sender.agent_text, seen
        assert seen.get("reply_receipt", {}).get("outcome") in {"accepted_current_turn", "accepted_inbox_read"}, seen
    return seen
