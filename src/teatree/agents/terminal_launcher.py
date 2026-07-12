"""Loopback ttyd web-terminal launcher (#3162 — resurrected from #541's `f7d65a9e~1`).

Spawns ``ttyd --writable --once`` on a free ``127.0.0.1`` port wrapping an agent
command and returns the ``launch_url``. This is the convenience debug tier: it
binds loopback ONLY (reached through the same SSH tunnel as the admin, never a
new network door), and ``--once`` makes the ttyd process die once the FIRST
client disconnects — so a connected-and-debugged session cleans itself up.

``--once`` alone does NOT bound a ttyd nobody ever connects to (it only fires on
DISCONNECT), so an unconnected ``--writable`` (== RCE-as-the-service-user)
listener would otherwise sit on loopback forever, and repeated dashboard clicks
would pile them up. ttyd has no native "exit if no client connects" flag, so a
per-spawn connect-grace reaper (:func:`_arm_connect_grace_reaper`) terminates an
unconnected ttyd after :data:`_CONNECT_GRACE_SECONDS`. A live session is spared —
the reaper checks for an ESTABLISHED client on the port first (fail-SAFE: an
inconclusive probe never reaps), so the legitimate connect-and-debug flow is
untouched while orphans are bounded.

The native-window / new-tab modes and the in-process registry the pre-#541 file
also carried are intentionally NOT resurrected: they served a local operator at
the machine, not a dashboard reached over a tunnel, and would be dead code here.
Break-glass when teatree itself is down stays host ``ssh`` + ``tmux`` (see
``docs/debug-runbook.md``) — a debug path served by the patient cannot treat it.
"""

import logging
import shutil
import threading
from dataclasses import dataclass

from teatree.utils.ports import find_free_port
from teatree.utils.run import DEVNULL, PIPE, Popen, run_allowed_to_fail, spawn

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_TTYD_INSTALL_HINT = "ttyd not found on PATH — install with `brew install ttyd` (macOS) or `apt install ttyd` (Linux)"
#: How long an unconnected ``--writable`` ttyd may sit with no client before it is
#: reaped. Generous enough for a human to click the dashboard button and connect,
#: short enough that an abandoned (or never-opened) one does not linger.
_CONNECT_GRACE_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class LaunchResult:
    launch_url: str = ""
    pid: int = 0
    error: str = ""


def _has_established_client(port: int) -> bool:
    """True iff an ESTABLISHED TCP client is connected to the loopback ttyd *port*.

    Fail-SAFE: any probe failure (lsof absent, errored, or timed out) is treated as
    connected, so a live debug session is never reaped on an inconclusive probe. The
    LISTEN socket itself is filtered out by ``-sTCP:ESTABLISHED``, so a match means a
    real client, not ttyd's own listener. Loopback-only.
    """
    try:
        result = run_allowed_to_fail(
            ["lsof", "-nP", f"-iTCP@{_LOOPBACK}:{port}", "-sTCP:ESTABLISHED"],
            expected_codes=(0, 1),  # lsof rc=1 == no matching connection (not an error)
            timeout=5.0,
        )
    except Exception:  # noqa: BLE001 — an unusable probe must not reap a possibly-live session; fail safe.
        logger.warning("ttyd connection probe failed for port %d; assuming connected (fail-safe)", port)
        return True
    return result.returncode == 0 and bool(result.stdout.strip())


def _reap_if_unconnected(proc: Popen[str], port: int) -> None:
    """Terminate *proc* iff it is still running with no client connected (an orphan).

    A no-op when the ttyd already exited (``--once`` fired on disconnect, or it
    errored out) or when a client is connected — a live session is left for
    ``--once`` to reap on its own disconnect.
    """
    if proc.poll() is not None:
        return
    if _has_established_client(port):
        return
    logger.info(
        "Reaping unconnected loopback ttyd (pid=%d, port=%d) after %.0fs grace", proc.pid, port, _CONNECT_GRACE_SECONDS
    )
    proc.terminate()


def _arm_connect_grace_reaper(proc: Popen[str], port: int) -> None:
    """Arm a daemon timer that reaps *proc* if no client connects within the grace window."""
    timer = threading.Timer(_CONNECT_GRACE_SECONDS, _reap_if_unconnected, args=(proc, port))
    timer.daemon = True
    timer.start()


def launch_ttyd(command: list[str]) -> LaunchResult:
    """Spawn ``ttyd --writable --once`` on a free loopback port wrapping *command*.

    Returns a :class:`LaunchResult` with the ``launch_url`` on success, or one
    carrying an ``error`` when ttyd is not installed — never raises, so a missing
    binary degrades to a rendered hint rather than a 500. A connect-grace reaper is
    armed so an unconnected ttyd is bounded, not left listening indefinitely.
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
    _arm_connect_grace_reaper(proc, port)
    logger.info("Launched loopback ttyd (pid=%d, port=%d)", proc.pid, port)
    return LaunchResult(launch_url=f"http://{_LOOPBACK}:{port}", pid=proc.pid)
