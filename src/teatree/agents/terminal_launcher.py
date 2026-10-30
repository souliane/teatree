"""Launch one loopback ttyd terminal on the dashboard's fixed forwarded port.

The secret base path and origin check protect the writable websocket. A supervisor
reaps exits immediately and terminates an unused listener after a grace period.
"""

import logging
import secrets
import shutil
import socket
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException

from teatree.utils.loopback_forward import TERMINAL_PORT
from teatree.utils.run import DEVNULL, Popen, TimeoutExpired, run_allowed_to_fail, spawn

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_TTYD_INSTALL_HINT = "ttyd not found on PATH — install with `brew install ttyd` (macOS) or `apt install ttyd` (Linux)"
#: How long an unconnected ``--writable`` ttyd may sit with no client before it is
#: reaped. Generous enough for a human to click the dashboard button and connect,
#: short enough that an abandoned (or never-opened) one does not linger.
_CONNECT_GRACE_SECONDS = 120.0
_STARTUP_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class LaunchResult:
    launch_url: str = ""
    pid: int = 0
    port: int = 0
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
    """Wait for exit and collect it; terminate an unconnected process after the grace period."""

    def supervise() -> None:
        try:
            proc.wait(timeout=_CONNECT_GRACE_SECONDS)
        except TimeoutExpired:
            _reap_if_unconnected(proc, port)
            proc.wait()

    threading.Thread(target=supervise, daemon=True).start()


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((_LOOPBACK, port))
        except OSError:
            return False
    return True


def _wait_for_ready(proc: Popen[str], port: int, path: str) -> bool:
    deadline = time.monotonic() + _STARTUP_SECONDS
    while time.monotonic() < deadline and proc.poll() is None:
        connection = HTTPConnection(_LOOPBACK, port, timeout=0.2)
        try:
            connection.request("GET", f"{path}/")
            if connection.getresponse().status == HTTPStatus.OK:
                return True
        except (OSError, HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.02)
    return False


def launch_ttyd(command: list[str], *, env: dict[str, str] | None = None, port: int = TERMINAL_PORT) -> LaunchResult:
    """Spawn one guarded ttyd on the fixed loopback port wrapping *command*.

    Returns a :class:`LaunchResult` with the ``launch_url`` on success, or one
    carrying an ``error`` when a required binary is absent or the port is busy.

    *env* is the child's whole environment, already resolved by the caller — which
    credential the wrapped command authenticates with is the caller's decision, not
    this launcher's. ``None`` inherits this process's environment.
    """
    ttyd_bin = shutil.which("ttyd")
    if not ttyd_bin:
        logger.error(_TTYD_INSTALL_HINT)
        return LaunchResult(error=_TTYD_INSTALL_HINT)

    if not shutil.which("lsof"):
        return LaunchResult(error="lsof not found on PATH — install lsof to supervise the terminal")
    if not _port_available(port):
        return LaunchResult(error=f"terminal port {port} is busy — close the existing terminal or wait two minutes")
    path = f"/{secrets.token_urlsafe(16)}"
    # Both streams go to DEVNULL: nothing reads ttyd's output, and an unread PIPE
    # can fill the OS pipe buffer and block a chatty ttyd mid-session. The reaper's
    # liveness probe is an independent loopback ``lsof`` on the port, not ttyd's stderr.
    proc = spawn(
        [
            ttyd_bin,
            "--writable",
            "--check-origin",
            "--max-clients",
            "1",
            "--interface",
            _LOOPBACK,
            "--port",
            str(port),
            "--base-path",
            path,
            "--once",
            *command,
        ],
        env=env,
        stdout=DEVNULL,
        stderr=DEVNULL,
    )
    _arm_connect_grace_reaper(proc, port)
    if not _wait_for_ready(proc, port, path):
        if proc.poll() is None:
            proc.terminate()
        return LaunchResult(error=f"ttyd could not start on port {port}")
    logger.info("Launched loopback ttyd (pid=%d, port=%d)", proc.pid, port)
    return LaunchResult(launch_url=f"http://{_LOOPBACK}:{port}{path}/", pid=proc.pid, port=port)
