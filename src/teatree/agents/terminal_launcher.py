"""Loopback ttyd web-terminal launcher (#3162 — resurrected from #541's `f7d65a9e~1`).

Spawns ``ttyd --writable --once`` on a free ``127.0.0.1`` port wrapping an agent
command and returns the ``launch_url``. This is the convenience debug tier: it
binds loopback ONLY (reached through the same SSH tunnel as the admin, never a
new network door), and ``--once`` makes the ttyd process die with the session, so
there is no lingering ``--writable`` (== RCE-as-the-service-user) terminal.

The native-window / new-tab modes and the in-process registry the pre-#541 file
also carried are intentionally NOT resurrected: they served a local operator at
the machine, not a dashboard reached over a tunnel, and would be dead code here.
Break-glass when teatree itself is down stays host ``ssh`` + ``tmux`` (see
``docs/debug-runbook.md``) — a debug path served by the patient cannot treat it.
"""

import logging
import shutil
from dataclasses import dataclass

from teatree.utils.ports import find_free_port
from teatree.utils.run import DEVNULL, PIPE, spawn

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_TTYD_INSTALL_HINT = "ttyd not found on PATH — install with `brew install ttyd` (macOS) or `apt install ttyd` (Linux)"


@dataclass(frozen=True, slots=True)
class LaunchResult:
    launch_url: str = ""
    pid: int = 0
    error: str = ""


def launch_ttyd(command: list[str]) -> LaunchResult:
    """Spawn ``ttyd --writable --once`` on a free loopback port wrapping *command*.

    Returns a :class:`LaunchResult` with the ``launch_url`` on success, or one
    carrying an ``error`` when ttyd is not installed — never raises, so a missing
    binary degrades to a rendered hint rather than a 500.
    """
    ttyd_bin = shutil.which("ttyd")
    if not ttyd_bin:
        logger.error(_TTYD_INSTALL_HINT)
        return LaunchResult(error=_TTYD_INSTALL_HINT)

    port = find_free_port(_LOOPBACK)
    proc = spawn(
        [ttyd_bin, "--writable", "--interface", _LOOPBACK, "--port", str(port), "--once", *command],
        stdout=DEVNULL,
        stderr=PIPE,
    )
    logger.info("Launched loopback ttyd (pid=%d, port=%d)", proc.pid, port)
    return LaunchResult(launch_url=f"http://{_LOOPBACK}:{port}", pid=proc.pid)
