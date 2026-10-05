"""PostToolUse reports settle before a turn completes, without ever waiting unboundedly."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents import codex_app_server_events, codex_app_server_options
from teatree.agents.codex_app_server_events import CodexToolEvents
from teatree.agents.codex_app_server_options import CodexAppServerOptions


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
