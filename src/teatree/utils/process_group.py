"""Stop an asyncio child that leads its own session, with everything it started."""

import asyncio
import os
import signal
from contextlib import suppress

_CLOSE_PIPES_SECONDS = 2


async def stop_process_group(process: asyncio.subprocess.Process) -> None:
    """SIGKILL the group *process* leads, reap it, and drain its pipes so asyncio closes the transports."""
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()
    streams = [stream for stream in (process.stdout, process.stderr) if stream is not None]
    with suppress(TimeoutError, OSError):
        await asyncio.wait_for(asyncio.gather(*(stream.read() for stream in streams)), timeout=_CLOSE_PIPES_SECONDS)
    await asyncio.sleep(0)
