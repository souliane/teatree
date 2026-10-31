"""Whether a pid still names a RUNNING process — a zombie is not one.

``os.kill(pid, 0)`` succeeds on a zombie: the kernel keeps the pid until a parent
waits for it. A grandchild killed after its own parent exited is re-parented to PID 1,
and in a container whose PID 1 never reaps (CI runs the suite under ``docker run``
without ``--init``) it stays a zombie for good. A probe on ``kill(0)`` alone then
reads a process the code under test DID kill as still alive. On a host, init reaps
it at once and both probes agree.
"""

import asyncio
import os
import shlex
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path
from unittest.mock import patch

GATED_FORKING_COMMAND = "mkfifo gate; echo $$ > leader; (cat gate > /dev/null; touch finished) & echo $! > child; wait"
_LEAVES_THE_SESSION = shlex.quote(
    "import os, time; os.setsid(); open('escapee', 'w').write(str(os.getpid())); time.sleep(8)"
)
HOLDS_THE_PIPES_FROM_ANOTHER_SESSION = f"{shlex.quote(sys.executable)} -c {_LEAVES_THE_SESSION} & sleep 30"


def is_running(pid: int) -> bool:
    """True while *pid* exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return not _is_zombie(pid)


def is_gone_within(pid: int, *, seconds: float) -> bool:
    """Poll until *pid* stops running; False when it still runs after *seconds*."""
    deadline = time.monotonic() + seconds
    while is_running(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def release_gate(directory: Path) -> None:
    """Open the gate ``GATED_FORKING_COMMAND`` waits on: a survivor leaves its mark, a dead group has no reader."""
    with suppress(OSError):
        os.close(os.open(directory / "gate", os.O_WRONLY | os.O_NONBLOCK))


def _written_pids(files: list[Path], *, seconds: float) -> list[int]:
    deadline = time.monotonic() + seconds
    while not all(file.exists() and file.read_text(encoding="utf-8").strip() for file in files):
        if time.monotonic() >= deadline:
            msg = f"{[file.name for file in files]} not written within {seconds}s"
            raise TimeoutError(msg)
        time.sleep(0.02)
    return [int(file.read_text(encoding="utf-8")) for file in files]


async def written_pids(directory: Path, *names: str, seconds: float = 10) -> list[int]:
    """The pids a command wrote to *names* under *directory*, awaited in a thread so the event loop keeps running."""
    return await asyncio.to_thread(_written_pids, [directory / name for name in names], seconds=seconds)


@contextmanager
def spawn_held_at_its_pipes() -> Generator[asyncio.Event]:
    """Hold a subprocess spawn on the running loop unfinished until the event is set, while its child already runs."""
    loop = asyncio.get_running_loop()
    connect = loop.connect_read_pipe
    released = asyncio.Event()

    async def connect_once_released(protocol_factory, pipe):
        await released.wait()
        return await connect(protocol_factory, pipe)

    with patch.object(loop, "connect_read_pipe", connect_once_released):
        yield released


def _is_zombie(pid: int) -> bool:
    # No /proc (macOS): its init reaps orphans promptly, so kill(0) alone is the answer there.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"
