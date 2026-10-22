"""Bind a running task to an addressable mailbox identity."""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.live_control import LiveTask
from teatree.agents.live_delivery import MailboxIdentity
from teatree.agents.live_mailbox import LiveMailboxBroker, PushOnlyTask, shared_broker
from teatree.utils.agent_spawn_marker import dispatched_agent_env

if TYPE_CHECKING:
    from claude_agent_sdk.types import McpServerConfig, McpStdioServerConfig

logger = logging.getLogger(__name__)


@contextmanager
def bound_task_mailbox(
    options: ClaudeAgentOptions,
    task: LiveTask,
    *,
    broker: LiveMailboxBroker | None = None,
) -> Generator[MailboxIdentity | None]:
    original = options.mcp_servers
    servers = original if isinstance(original, dict) else {}
    teatree = servers.get("teatree")
    teatree_stdio = (
        cast("McpStdioServerConfig", teatree)
        if isinstance(teatree, dict) and isinstance(teatree.get("command"), str)
        else None
    )
    label = f"{task.phase} task {task.pk}"
    child_env: dict[str, str] = {}
    if teatree_stdio is not None:
        env = teatree_stdio.get("env")
        child_env = {**(env if isinstance(env, dict) else {}), **dispatched_agent_env()}
        options.mcp_servers = cast(
            "dict[str, McpServerConfig]",
            {**servers, "teatree": {**teatree_stdio, "env": child_env}},
        )
    if teatree_stdio is None and task.harness != "codex_app_server":
        try:
            yield None
        finally:
            options.mcp_servers = original
        return
    active: LiveMailboxBroker | None = None
    identity: MailboxIdentity | None = None
    try:
        try:
            active = broker or shared_broker()
            identity = active.register(
                room=str(task.ticket),
                harness=task.harness,
                label=label,
                task=task if teatree_stdio is not None else PushOnlyTask(task),
            )
        except Exception:
            logger.warning("live mailbox unavailable for %s; dispatching without one", label, exc_info=True)
        if identity is not None and teatree_stdio is not None:
            options.mcp_servers = cast(
                "dict[str, McpServerConfig]",
                {**servers, "teatree": {**teatree_stdio, "env": {**child_env, **identity.mcp_env()}}},
            )
        yield identity
    finally:
        options.mcp_servers = original
        if identity is not None and active is not None:
            active.unregister(identity.address)
