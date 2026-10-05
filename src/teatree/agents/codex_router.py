"""Run the hook router for one Codex action, killing it and its children whenever the caller stops waiting."""

import asyncio
import json
import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TEATREE_ROOT = Path(__file__).resolve().parents[3]
_RUN_HOOK = _TEATREE_ROOT / "hooks" / "scripts" / "run-hook.sh"
_ROUTER = _TEATREE_ROOT / "hooks" / "scripts" / "hook_router.py"
_TIMEOUT_SECONDS = 30
_CLOSE_PIPES_SECONDS = 2


@dataclass(frozen=True, slots=True)
class RouterRun:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def crashed(self) -> bool:
        return b"Traceback (most recent call last)" in self.stderr or b"hooks are DISABLED" in self.stderr


async def run_router(event: str, payload: Mapping[str, Any], cwd: str) -> RouterRun | None:
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
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            await _close_pipes(process)
    return RouterRun(returncode, stdout, stderr)


async def _close_pipes(process: asyncio.subprocess.Process) -> None:
    """Read the killed group's pipes to EOF so asyncio closes the transport before the loop can end."""
    streams = [stream for stream in (process.stdout, process.stderr) if stream is not None]
    with suppress(TimeoutError, OSError):
        await asyncio.wait_for(asyncio.gather(*(stream.read() for stream in streams)), timeout=_CLOSE_PIPES_SECONDS)
    await asyncio.sleep(0)
