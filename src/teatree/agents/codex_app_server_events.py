"""Keep approval policy and completed-tool reports aligned with Codex thread events."""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from teatree.agents.codex_app_server_options import CodexAppServerOptions
from teatree.agents.codex_post_tool import report_completed_item

logger = logging.getLogger(__name__)

POST_SETTLE_SECONDS = 30.0


class CodexToolEvents:
    def __init__(self) -> None:
        self.approval_options: dict[str, CodexAppServerOptions] = {}
        self.file_changes: dict[tuple[str, str], object] = {}
        self.post_tasks: dict[str, asyncio.Task[None]] = {}
        self.child_parent: dict[str, str] = {}

    def register(self, thread_id: str, options: CodexAppServerOptions) -> None:
        self.approval_options[thread_id] = options

    def unregister(self, thread_id: str) -> None:
        for child in tuple(self.child_parent):
            if self.child_parent[child] == thread_id:
                self.unregister(child)
                self.child_parent.pop(child, None)
        self.approval_options.pop(thread_id, None)
        if task := self.post_tasks.get(thread_id):
            if task.done():
                self.post_tasks.pop(thread_id, None)
            else:

                def forget(finished: asyncio.Task[None]) -> None:
                    if self.post_tasks.get(thread_id) is finished:
                        self.post_tasks.pop(thread_id, None)

                task.add_done_callback(forget)
        for key in tuple(self.file_changes):
            if key[0] == thread_id:
                self.file_changes.pop(key, None)

    def observe(self, message: Mapping[str, Any]) -> None:
        params = message.get("params")
        if not isinstance(params, dict):
            return
        thread_id = params.get("threadId")
        options = self.approval_options.get(thread_id)
        if options is None:
            return
        method = message.get("method")
        item = params.get("item")
        if method in {"item/started", "item/completed"} and isinstance(item, dict):
            self._observe_item(method, item, thread_id, options)
        if options.sandbox_mode != "danger-full-access":
            return
        if method == "item/fileChange/patchUpdated":
            self.file_changes[thread_id, str(params.get("itemId", ""))] = params.get("changes")
        elif method == "item/started" and isinstance(item, dict) and item.get("type") == "fileChange":
            self.file_changes[thread_id, str(item.get("id", ""))] = item.get("changes")

    def _observe_item(self, method: str, item: dict[str, Any], thread_id: str, options: CodexAppServerOptions) -> None:
        receivers = item.get("receiverThreadIds") if item.get("type") == "collabAgentToolCall" else None
        if isinstance(receivers, list):
            for child in receivers:
                if isinstance(child, str) and child and child not in self.approval_options:
                    self.register(child, options)
                    self.child_parent[child] = thread_id
        if method == "item/completed":
            self.file_changes.pop((thread_id, str(item.get("id", ""))), None)
            if options.sandbox_mode == "danger-full-access" and item.get("type") in {"commandExecution", "fileChange"}:
                previous = self.post_tasks.get(thread_id)
                self.post_tasks[thread_id] = asyncio.create_task(self._report_after(previous, item, thread_id, options))

    async def wait_for_post(self, thread_id: str) -> None:
        if (task := self.post_tasks.get(thread_id)) is not None:
            finished, _pending = await asyncio.wait({task}, timeout=POST_SETTLE_SECONDS)
            if not finished:
                logger.warning("Codex PostToolUse reports for a thread are still running; not waiting longer")

    async def drain_post_tasks(self) -> None:
        pending = tuple(self.post_tasks.values())
        if not pending:
            return
        _finished, stragglers = await asyncio.wait(pending, timeout=POST_SETTLE_SECONDS)
        for task in stragglers:
            task.cancel()
        for outcome in await asyncio.gather(*pending, return_exceptions=True):
            if isinstance(outcome, Exception):
                logger.warning("Codex PostToolUse report failed: %s", type(outcome).__name__)

    @staticmethod
    async def _report_after(
        previous: asyncio.Task[None] | None, item: Mapping[str, Any], thread_id: str, options: CodexAppServerOptions
    ) -> None:
        if previous is not None:
            (earlier,) = await asyncio.gather(previous, return_exceptions=True)
            if isinstance(earlier, asyncio.CancelledError):
                raise earlier
            if isinstance(earlier, Exception):
                logger.warning("Codex PostToolUse report failed: %s", type(earlier).__name__)
        try:
            await report_completed_item(item, thread_id, options)
        except (OSError, ValueError, TypeError, RuntimeError):
            logger.warning("Codex PostToolUse router failed for completed item")
