"""Shared Redis container managed by teatree.

Teatree runs a single Redis container (`teatree-redis`) on localhost:6379.
Each ticket gets its own Redis DB index for isolation. Slot count is
`teatree.redis_db_count` in `~/.teatree.toml` (default 16). Slots are
allocated by `Ticket.objects.allocate_redis_slot()` and released (with
`FLUSHDB`) on teardown.
"""

import logging
import shutil
import subprocess

from teatree.config import load_config

logger = logging.getLogger(__name__)

CONTAINER_NAME = "teatree-redis"
IMAGE = "redis:7-alpine"
HOST_PORT = 6379


def redis_db_count() -> int:
    """Return the number of Redis DB slots (``~/.teatree.toml``, default 16)."""
    return load_config().user.redis_db_count


def _docker() -> str:
    path = shutil.which("docker")
    if not path:
        msg = "docker CLI not found on PATH"
        raise RuntimeError(msg)
    return path


def _run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([_docker(), *args], check=check, capture_output=capture)


def status() -> str:
    """Return 'running', 'stopped', or 'missing'."""
    result = _run(
        "inspect",
        "-f",
        "{{.State.Status}}",
        CONTAINER_NAME,
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return "missing"
    return result.stdout.decode().strip() or "missing"


def ensure_running() -> None:
    """Start the shared Redis container if not already running."""
    current = status()
    if current == "running":
        return
    if current == "missing":
        logger.info("Creating %s container on :%d", CONTAINER_NAME, HOST_PORT)
        _run(
            "run",
            "-d",
            "--name",
            CONTAINER_NAME,
            "-p",
            f"{HOST_PORT}:6379",
            "--restart",
            "unless-stopped",
            IMAGE,
            "redis-server",
            "--databases",
            str(redis_db_count()),
        )
        return
    logger.info("Starting existing %s container (status=%s)", CONTAINER_NAME, current)
    _run("start", CONTAINER_NAME)


def stop() -> None:
    """Stop the shared Redis container (no-op if missing)."""
    if status() == "missing":
        return
    _run("stop", CONTAINER_NAME, check=False)


def flushdb(index: int) -> None:
    """FLUSHDB on the given Redis DB index.

    Called when a ticket releases its slot so the next ticket to grab the
    slot starts with a clean cache/queue.
    """
    count = redis_db_count()
    if not 0 <= index < count:
        msg = f"redis db index {index} out of range 0..{count - 1}"
        raise ValueError(msg)
    if status() != "running":
        logger.debug("Skipping flushdb(%d): %s not running", index, CONTAINER_NAME)
        return
    _run(
        "exec",
        CONTAINER_NAME,
        "redis-cli",
        "-n",
        str(index),
        "FLUSHDB",
        check=False,
    )
