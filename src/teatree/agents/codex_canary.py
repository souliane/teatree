"""One turn through the stored Codex login, under the lock a real turn takes, asking for the word OK."""

import asyncio
import tempfile
from pathlib import Path

from claude_agent_sdk import AssistantMessage

from teatree.agents.codex_app_server import CodexAppServerSession
from teatree.agents.codex_app_server_lifecycle import opened_session
from teatree.agents.codex_app_server_options import CodexAppServerError, CodexAppServerOptions
from teatree.agents.codex_auth_cache import CodexAuthCache, CodexAuthCacheError
from teatree.agents.codex_read_only_executor import EXECUTOR_PHASES, ExecutorRefusalError, attest
from teatree.agents.codex_read_only_server import ExecutorCodexAppServer
from teatree.agents.codex_read_only_verdict import WORKTREE_ROOT
from teatree.agents.codex_shared_app_server import SharedCodexSession
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.harness_registry import HarnessFallbackError

LOCK_WAIT_SECONDS = 60.0
_PROMPT = "Reply with the single word OK. Do not use any tool."


async def _answered(code_home: Path, cwd: str) -> bool:
    options = CodexAppServerOptions(
        core=HarnessOptions(cwd=cwd),
        runtime_workspace_roots=(cwd,),
        sandbox_mode="read-only",
        sandbox_policy={"type": "readOnly"},
        config={"features": {"apps": False, "multi_agent": False}},
    )
    session = CodexAppServerSession(options, resume=None, code_home=code_home)
    async with CodexAuthCache(code_home).session(lock_timeout=LOCK_WAIT_SECONDS):
        await session.start()
        try:
            await session.query(_PROMPT)
            return any([isinstance(message, AssistantMessage) async for message in session.receive_response()])
        finally:
            await session.close()


def canary_failure(code_home: Path) -> str | None:
    """``None`` when the login answered; otherwise a reason that never carries the login's contents."""
    with tempfile.TemporaryDirectory(prefix="t3-codex-canary-") as cwd:
        try:
            answered = asyncio.run(_answered(code_home, cwd))
        except (CodexAuthCacheError, CodexAppServerError, HarnessFallbackError) as exc:
            return str(exc)
    return None if answered else "Codex finished the turn without an assistant message"


async def _executor_answered() -> bool:
    attestation = await asyncio.to_thread(attest, min(EXECUTOR_PHASES), ())
    options = CodexAppServerOptions(
        core=HarnessOptions(cwd=WORKTREE_ROOT),
        runtime_workspace_roots=(),
        sandbox_mode="danger-full-access",
        sandbox_policy={"type": "dangerFullAccess"},
        config={
            "features": {"apps": False, "multi_agent": False},
            "projects": {WORKTREE_ROOT: {"trust_level": "untrusted"}},
        },
        executor=attestation,
    )
    manager = ExecutorCodexAppServer(attestation)
    session = SharedCodexSession(options, manager=manager, resume=None)
    try:
        async with opened_session(session):
            await session.query(_PROMPT)
            return any([isinstance(message, AssistantMessage) async for message in session.receive_response()])
    finally:
        await asyncio.to_thread(manager.close)


def executor_canary_failure() -> str | None:
    """The same OK-turn through the read-only executor's own login, from a fresh home it removes after."""
    try:
        answered = asyncio.run(_executor_answered())
    except (ExecutorRefusalError, CodexAuthCacheError, CodexAppServerError, HarnessFallbackError) as exc:
        return str(exc)
    return None if answered else "Codex finished the turn without an assistant message"
