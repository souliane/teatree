"""Stop an asyncio child that leads its own session, with everything it started."""

import asyncio
import os
import signal
from collections.abc import Awaitable
from contextlib import suppress

_CLOSE_PIPES_SECONDS = 0.25
_EXIT_POLL_SECONDS = 0.005


def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        # A container without the privilege to signal the group still owns the child itself.
        with suppress(ProcessLookupError):
            process.kill()


async def spawn_group_leader(spawn: Awaitable[asyncio.subprocess.Process]) -> asyncio.subprocess.Process:
    """Await *spawn* of a session leader; a cancel landing mid-spawn stops the whole group, not just the leader.

    asyncio's own cleanup of a cancelled spawn kills only the leader, so anything it already forked would live on.
    """
    spawning = asyncio.ensure_future(spawn)
    try:
        return await asyncio.shield(spawning)
    except asyncio.CancelledError:
        await asyncio.wait([spawning])
        if not spawning.cancelled() and spawning.exception() is None:
            await stop_process_group(spawning.result())
        raise


async def stop_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the group *process* leads and return once its leader has an exit status, whoever holds its pipes.

    ``Process.wait()`` called before the exit returns only once the pipes close, which a descendant
    outside the group keeps open for as long as it lives, so each wait is short and ``returncode``
    ends the loop. The pipes then get a brief grace to close so asyncio can release their transports.
    """
    _kill_group(process)
    while process.returncode is None:
        with suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=_EXIT_POLL_SECONDS)
    streams = [stream for stream in (process.stdout, process.stderr) if stream is not None]
    with suppress(TimeoutError, OSError):
        await asyncio.wait_for(asyncio.gather(*(stream.read() for stream in streams)), timeout=_CLOSE_PIPES_SECONDS)
