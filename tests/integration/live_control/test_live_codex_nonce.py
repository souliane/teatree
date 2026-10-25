"""Opt-in Claude-to-Codex mailbox run against authenticated worker harnesses."""

import asyncio
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase

from teatree.agents._runner_options import _wire_teatree_mcp_server
from teatree.agents.codex_app_server import CodexAppServerHarness
from teatree.agents.codex_shared_app_server import SharedCodexSession
from teatree.agents.compaction_guard import CompactionGuard, GuardedHarness
from teatree.agents.harness import ClaudeSdkHarness
from teatree.agents.live_client import LiveClient, LiveOfflineError
from teatree.agents.live_delivery import PeerDelivery, SendReceipt
from teatree.agents.live_mailbox import _Participant, shared_broker
from teatree.agents.model_tiering import tier_models
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import Session, Task
from teatree.credential_config import resolve_api_key_credential, resolve_subscription_credential
from tests.factories import planned_ticket


def _runtime() -> HeartbeatRuntime:
    return HeartbeatRuntime(
        watchdog=LoopWatchdog(max_runtime_seconds=240, max_turns=0, max_cost_usd=0.0),
        heartbeat_interval=60,
        sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
        renew_lease=lambda _task: None,
        drain_reason=lambda: "",
    )


def _wait_for_codex(task_pk: int) -> None:
    client = LiveClient()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            ready = any(row["task"] == task_pk and row["steerable"] for row in client.sessions())
        except LiveOfflineError:
            ready = False
        if ready:
            return
        time.sleep(0.2)
    msg = "Codex did not become steerable before the Claude launch deadline"
    raise TimeoutError(msg)


def _claude_credential() -> tuple[str, str]:
    if os.environ.get("T3_LIVE_NONCE_CREDENTIAL") == "api_key":
        return "ANTHROPIC_API_KEY", resolve_api_key_credential().resolve()
    return "CLAUDE_CODE_OAUTH_TOKEN", resolve_subscription_credential().resolve()


@contextmanager
def _capture_admission(nonce: str, seen: dict[str, Any]) -> Generator[None]:
    broker = shared_broker()
    original_accepted = broker._accepted
    original_request = SharedCodexSession._request

    async def record_accepted(delivery: PeerDelivery, recipient: _Participant | None, wait: float) -> SendReceipt:
        receipt = await original_accepted(delivery, recipient, wait)
        if delivery.text == nonce:
            seen["receipt"] = receipt
        return receipt

    async def record_request(session: SharedCodexSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            answer = await original_request(session, method, params)
        except Exception as exc:
            if method == "turn/steer":
                seen["turn_steer_error"] = type(exc).__name__
            raise
        if method == "turn/steer":
            seen["turn_steer_answer"] = {"turnId_matches_current": answer.get("turnId") == params["expectedTurnId"]}
        return answer

    with (
        patch.object(SharedCodexSession, "_request", record_request),
        patch.object(broker, "_accepted", record_accepted),
    ):
        yield


@pytest.mark.integration
@pytest.mark.timeout(300)
@pytest.mark.usefixtures("pass_store")
class LiveCodexNonceTests(TestCase):
    def test_claude_mailbox_message_enters_the_running_codex_turn(self) -> None:
        if os.environ.get("T3_LIVE_CODEX_NONCE") != "1":
            self.skipTest("set T3_LIVE_CODEX_NONCE=1 to spend real Claude and Codex turns")

        credential_env, credential = _claude_credential()
        ticket = planned_ticket()
        codex_task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        claude_task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        nonce = secrets.token_hex(12)
        cwd = tempfile.mkdtemp(prefix="t3-live-codex-nonce-")
        codex_options = ClaudeAgentOptions(
            model="gpt-5.6-sol", permission_mode="bypassPermissions", cwd=cwd, setting_sources=[], mcp_servers={}
        )
        claude_options = ClaudeAgentOptions(
            model=tier_models()["cheap"],
            permission_mode="bypassPermissions",
            cwd=cwd,
            setting_sources=[],
            max_turns=8,
            env={credential_env: credential},
        )
        _wire_teatree_mcp_server(claude_options, phase="coding")
        codex_harness = GuardedHarness(
            CodexAppServerHarness(refusal=None, code_home=Path(tempfile.mkdtemp(prefix="t3-live-codex-auth-"))),
            CompactionGuard(),
            "codex_app_server",
        )
        seen: dict[str, Any] = {}

        def send_from_claude() -> None:
            try:
                _wait_for_codex(codex_task.pk)
                prompt = (
                    "Call mcp__teatree__agent_mailbox_peers to find the live codex_app_server peer, then "
                    f"call mcp__teatree__agent_mailbox_send to send exactly this text: {nonce}. "
                    "Choose a fresh key. Report the complete receipt in your final answer."
                )
                seen["claude"] = asyncio.run(
                    drive_with_heartbeat(
                        claude_task,
                        prompt,
                        claude_options,
                        GuardedHarness(ClaudeSdkHarness(), CompactionGuard(), "claude_sdk"),
                        runtime=_runtime(),
                    )
                )
            except (LiveOfflineError, OSError, RuntimeError, TimeoutError) as exc:
                seen["claude_error"] = type(exc).__name__

        sender = threading.Thread(target=send_from_claude, daemon=True)
        sender.start()
        with _capture_admission(nonce, seen):
            codex = asyncio.run(
                drive_with_heartbeat(
                    codex_task,
                    "Run the shell command `sleep 90` once. Then include any TeaTree peer nonce you received "
                    "in your final sentence.",
                    codex_options,
                    codex_harness,
                    runtime=_runtime(),
                )
            )
        sender.join(timeout=120)
        claude = seen.get("claude")
        evidence = {
            "receipt_accepted_current_turn": seen.get("receipt", {}).get("outcome") == "accepted_current_turn",
            "turn_steer_answer": seen.get("turn_steer_answer"),
            "turn_steer_error": seen.get("turn_steer_error"),
            "codex_observed_nonce": nonce in codex.agent_text,
            "codex_result": codex.result_message is not None,
            "claude_result": claude is not None and claude.result_message is not None,
            "claude_error": seen.get("claude_error"),
            "claude_tool_calls": claude.tool_calls if claude is not None else None,
            "claude_stuck_reason": claude.stuck_reason if claude is not None else None,
            "claude_result_subtype": claude.result_message.subtype
            if claude is not None and claude.result_message is not None
            else None,
            "claude_text_length": len(claude.agent_text) if claude is not None else None,
            "claude_mentions_mailbox": claude is not None and "mailbox" in claude.agent_text.lower(),
            "claude_mentions_inactive": claude is not None and "recipient is not active" in claude.agent_text.lower(),
        }
        sys.stdout.write(json.dumps(evidence) + "\n")
        assert evidence["receipt_accepted_current_turn"]
        assert evidence["turn_steer_answer"] == {"turnId_matches_current": True}
        assert evidence["codex_observed_nonce"]
