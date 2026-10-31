"""Run the hook router for one Codex action, killing it and its children whenever the caller stops waiting."""

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teatree.agents.codex_app_server_env import router_process_env
from teatree.forge_credentials import CODEX_ROUTER_ENV
from teatree.utils.process_group import stop_process_group

_TEATREE_ROOT = Path(__file__).resolve().parents[3]
_RUN_HOOK = _TEATREE_ROOT / "hooks" / "scripts" / "run-hook.sh"
_ROUTER = _TEATREE_ROOT / "hooks" / "scripts" / "hook_router.py"
_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class RouterRun:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def crashed(self) -> bool:
        return b"Traceback (most recent call last)" in self.stderr or b"hooks are DISABLED" in self.stderr


async def run_router(
    event: str, payload: Mapping[str, Any], cwd: str, *, dispatch_env: Mapping[str, str] | None = None
) -> RouterRun | None:
    """The router's exit, stdout and stderr, or ``None`` when it could not start or timed out."""
    process: asyncio.subprocess.Process | None = None
    finished = False
    try:
        process = await asyncio.create_subprocess_exec(
            str(_RUN_HOOK),
            str(_ROUTER),
            "--event",
            event,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**router_process_env(dispatch_env), CODEX_ROUTER_ENV: "1"},
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps({**payload, "hook_event_name": event, "cwd": cwd}).encode()),
            timeout=_TIMEOUT_SECONDS,
        )
        returncode = await process.wait()
        finished = True
    except (OSError, TimeoutError):
        return None
    finally:
        if process is not None and not finished:
            await stop_process_group(process)
    return RouterRun(returncode, stdout, stderr)
