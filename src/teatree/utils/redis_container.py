"""Shared Redis container managed by teatree.

Teatree runs a single Redis container (`teatree-redis`) on localhost:6379.
Each ticket gets its own Redis DB index for isolation. Slot count is
`teatree.redis_db_count` in `~/.teatree.toml` (default 16). Slots are
allocated by `Ticket.objects.allocate_redis_slot()` and released (with
`FLUSHDB`) on teardown.
"""

import logging
import shutil
from subprocess import CompletedProcess

from teatree.config import load_config
from teatree.utils.run import run_allowed_to_fail, run_checked

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


def _docker_checked(*args: str) -> CompletedProcess[str]:
    return run_checked([_docker(), *args])


def _docker_tolerant(*args: str) -> CompletedProcess[str]:
    return run_allowed_to_fail([_docker(), *args], expected_codes=None)


def status() -> str:
    """Return 'running', 'stopped', or 'missing'."""
    result = _docker_tolerant("inspect", "-f", "{{.State.Status}}", CONTAINER_NAME)
    if result.returncode != 0:
        return "missing"
    return result.stdout.strip() or "missing"


def ensure_running() -> None:
    """Start the shared Redis container if not already running."""
    current = status()
    if current == "running":
        return
    if current == "missing":
        logger.info("Creating %s container on :%d", CONTAINER_NAME, HOST_PORT)
        _docker_checked(
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
    _docker_checked("start", CONTAINER_NAME)


def stop() -> None:
    """Stop the shared Redis container (no-op if missing)."""
    if status() == "missing":
        return
    _docker_tolerant("stop", CONTAINER_NAME)


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
    _docker_tolerant("exec", CONTAINER_NAME, "redis-cli", "-n", str(index), "FLUSHDB")
