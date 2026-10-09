"""Bounded coding turn through the same harness used by task dispatch."""

import asyncio
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import SystemPromptPreset

from teatree.agents._runner_env import system_child_env
from teatree.agents.compaction_guard import COMPACTION_BLOCKED_REASON, CompactionGuard, with_compaction_off
from teatree.agents.harness import Harness, resolve_harness


def run_bounded_write_turn(prompt: str, cwd: Path, *, timeout_seconds: float, harness: Harness | None = None) -> None:
    """Dispatch a write-capable turn through the configured coding harness."""
    resolved = harness if harness is not None else resolve_harness(phase="coding")
    guard = CompactionGuard()
    child_env = system_child_env() if resolved.capabilities.spawns_cli_child else None
    options = with_compaction_off(
        ClaudeAgentOptions(
            system_prompt=SystemPromptPreset(type="preset", preset="claude_code", append=prompt),
            cwd=str(cwd),
            add_dirs=[str(cwd)],
            permission_mode="bypassPermissions",
            disallowed_tools=["AskUserQuestion"],
            max_turns=0,
            env=child_env or {},
        ),
        guard,
    )

    async def drive() -> None:
        async with asyncio.timeout(timeout_seconds), resolved.open(options) as session:
            guard.arm(session.interrupt)
            await session.query(prompt)
            async for _message in session.receive_response():
                pass

    asyncio.run(drive())
    if guard.stopped_run:
        raise RuntimeError(COMPACTION_BLOCKED_REASON)
