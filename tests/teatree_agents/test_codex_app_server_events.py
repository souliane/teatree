"""PostToolUse reports settle before a turn completes, without ever waiting unboundedly."""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents import codex_app_server_events, codex_app_server_options
from teatree.agents.codex_app_server_events import CodexToolEvents, dynamic_mailbox_tools, mailbox_tool_result
from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.live_mailbox import LiveMailboxBroker
from teatree.agents.live_mailbox_client import MailboxClient


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (None, {"agent_mailbox_self", "agent_mailbox_peers"}),
        (
            "planning",
            {
                "agent_mailbox_self",
                "agent_mailbox_peers",
                "agent_mailbox_send",
                "agent_mailbox_inbox",
                "agent_mailbox_wait",
            },
        ),
        (
            "coding",
            {
                "agent_mailbox_self",
                "agent_mailbox_peers",
                "agent_mailbox_send",
                "agent_mailbox_inbox",
                "agent_mailbox_wait",
            },
        ),
        (
            "shipping",
            {
                "agent_mailbox_self",
                "agent_mailbox_peers",
                "agent_mailbox_send",
                "agent_mailbox_inbox",
                "agent_mailbox_wait",
            },
        ),
    ],
)
def test_dynamic_mailbox_tool_grants_cover_read_only_and_mcp_write_phases(
    phase: str | None, expected: set[str]
) -> None:
    assert {tool["name"] for tool in dynamic_mailbox_tools(phase)} == expected


def test_namespaced_codex_tool_call_cannot_reach_the_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work", permission_mode="bypassPermissions")
    )
    mailbox = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    options = replace(options, core=replace(options.core, mailbox=mailbox), mailbox_phase="coding")
    request = {"namespace": "other", "threadId": "thread-1", "tool": "agent_mailbox_self", "arguments": {}}

    assert asyncio.run(mailbox_tool_result(request, {"thread-1": options})) is None


def test_codex_mailbox_self_accepts_null_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)
    with LiveMailboxBroker(runtime_dir=tmp_path) as broker:
        identity = broker.register(room="ticket", harness="codex_app_server", label="coder")
        options = CodexAppServerOptions.from_sdk_options(
            ClaudeAgentOptions(cwd="/work", permission_mode="bypassPermissions")
        )
        mailbox = MailboxClient(identity.socket_path, identity.token)
        options = replace(options, core=replace(options.core, mailbox=mailbox), mailbox_phase="coding")
        request = {"threadId": "thread-1", "tool": "agent_mailbox_self", "arguments": None}

        result = asyncio.run(mailbox_tool_result(request, {"thread-1": options}))

    assert result is not None
    assert result["success"] is True
    assert identity.address in result["contentItems"][0]["text"]


@pytest.mark.parametrize("error_kind", ["known", "unexpected"])
def test_codex_mailbox_tool_returns_a_failed_result_on_transport_error(
    monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work", permission_mode="bypassPermissions")
    )
    mailbox = MailboxClient(Path("/tmp/no-such-mailbox.sock"), "private-token")
    options = replace(options, core=replace(options.core, mailbox=mailbox), mailbox_phase="coding")
    request = {"threadId": "thread-1", "tool": "agent_mailbox_self", "arguments": {}}

    if error_kind == "unexpected":
        with patch("asyncio.open_unix_connection", side_effect=RuntimeError("unexpected transport failure")):
            result = asyncio.run(mailbox_tool_result(request, {"thread-1": options}))
    else:
        result = asyncio.run(mailbox_tool_result(request, {"thread-1": options}))

    assert result is not None
    assert result["success"] is False
    assert result["contentItems"][0]["text"] == (
        "Mailbox tool failed"
        if error_kind == "unexpected"
        else "Live mailbox is unavailable; this TeaTree session may have ended"
    )


@pytest.fixture(autouse=True)
def _short_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_app_server_events, "POST_SETTLE_SECONDS", 0.2)


async def _never() -> None:
    await asyncio.Event().wait()


def test_waiting_for_a_threads_reports_gives_up_after_the_settle_bound() -> None:
    async def run() -> None:
        events = CodexToolEvents()
        events.post_tasks["thread-1"] = asyncio.create_task(_never())
        try:
            await asyncio.wait_for(events.wait_for_post("thread-1"), timeout=5)
        finally:
            events.post_tasks["thread-1"].cancel()

    asyncio.run(run())


def test_draining_at_close_cancels_reports_that_outlast_the_bound() -> None:
    async def run() -> bool:
        events = CodexToolEvents()
        stuck = asyncio.create_task(_never())
        events.post_tasks["thread-1"] = stuck
        await asyncio.wait_for(events.drain_post_tasks(), timeout=5)
        return stuck.cancelled()

    assert asyncio.run(run()) is True


def test_draining_lets_reports_that_finish_in_time_complete() -> None:
    async def run() -> bool:
        events = CodexToolEvents()
        quick = asyncio.create_task(asyncio.sleep(0))
        events.post_tasks["thread-1"] = quick
        await events.drain_post_tasks()
        return quick.done() and not quick.cancelled()

    assert asyncio.run(run()) is True


def test_one_report_that_fails_does_not_abort_the_reports_after_it_and_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    reported: list[str] = []

    async def report(item: Mapping[str, Any], _thread_id: str, _options: CodexAppServerOptions) -> None:
        await asyncio.sleep(0)
        if item["id"] == "first":
            raise KeyError(item["id"])
        reported.append(str(item["id"]))

    monkeypatch.setattr(codex_app_server_events, "report_completed_item", report)
    monkeypatch.setattr(codex_app_server_options, "container_is_the_sandbox", lambda: True)
    options = CodexAppServerOptions.from_sdk_options(
        ClaudeAgentOptions(cwd="/work", permission_mode="bypassPermissions")
    )

    async def run() -> None:
        events = CodexToolEvents()
        events.register("thread-1", options)
        for item_id in ("first", "second"):
            events.observe(
                {
                    "method": "item/completed",
                    "params": {"threadId": "thread-1", "item": {"id": item_id, "type": "commandExecution"}},
                }
            )
        await events.wait_for_post("thread-1")

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())

    assert reported == ["second"]
    assert "Codex PostToolUse report failed: KeyError" in caplog.text
