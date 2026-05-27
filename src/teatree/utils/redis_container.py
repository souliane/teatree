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

from teatree.utils.run import run_allowed_to_fail, run_checked

logger = logging.getLogger(__name__)

CONTAINER_NAME = "teatree-redis"
IMAGE = "redis:7-alpine"
HOST_PORT = 6379


DEFAULT_DB_COUNT = 16


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


def _host_port_published() -> bool:
    """True when ``teatree-redis`` publishes 6379 to the host.

    A container created (by an older teatree, or any non-publishing path)
    without ``-p 6379:6379`` answers ``docker port`` with no mapping. Such a
    container is "running" but every worktree's web service reaches Redis via
    ``host.docker.internal:6379`` — unpublished, that connection is refused
    and every request that touches the cache/broker 500s (Error 111).
    """
    result = _docker_tolerant("port", CONTAINER_NAME, "6379")
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())


def _non_teatree_squatters_on_host_port() -> list[str]:
    """Return names of non-``teatree-redis`` containers publishing host 6379.

    A legacy overlay-managed container from a ``docker-compose.yml``
    predating the ``profiles: [disabled]`` reset keeps the host port
    bound. Without eviction, ``_create()`` fails with
    ``bind: address already in use`` and ``teatree-redis`` stays in
    ``Created`` state — every worktree's cache/broker traffic 500s (#1373).
    """
    result = _docker_tolerant("ps", "--filter", f"publish={HOST_PORT}", "--format", "{{.Names}}")
    if result.returncode != 0:
        return []
    return [name for name in result.stdout.splitlines() if name.strip() and name.strip() != CONTAINER_NAME]


def _evict_squatters() -> None:
    """Stop and remove any non-``teatree-redis`` container holding host 6379."""
    for name in _non_teatree_squatters_on_host_port():
        logger.info("Evicting legacy container %s squatting on host port %d", name, HOST_PORT)
        _docker_tolerant("stop", name)
        _docker_tolerant("rm", name)


def _create(db_count: int) -> None:
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
        str(db_count),
    )


def _recreate_with_port_publish(db_count: int) -> None:
    logger.info(
        "%s lacks the :%d host publish — recreating to reconcile the port mapping",
        CONTAINER_NAME,
        HOST_PORT,
    )
    _docker_tolerant("stop", CONTAINER_NAME)
    _docker_tolerant("rm", CONTAINER_NAME)
    _create(db_count)


def ensure_running(db_count: int = DEFAULT_DB_COUNT) -> None:
    """Start the shared Redis container, self-healing the host port publish.

    Idempotent: a *running* container that lacks the ``-p 6379:6379`` publish
    is reconciled (recreated), not left as-is. Without this, a container
    created by an older code path stays "running" forever while every
    worktree's ``host.docker.internal:6379`` is unreachable and every
    cache/broker-touching request 500s. A non-``teatree-redis`` container
    squatting on host port 6379 is evicted before any create/recreate so
    ``docker run -p 6379:6379`` doesn't fail with ``address already in use``.
    """
    current = status()
    if current == "running":
        if not _host_port_published():
            _evict_squatters()
            _recreate_with_port_publish(db_count)
        return
    if current == "missing":
        _evict_squatters()
        _create(db_count)
        return
    logger.info("Starting existing %s container (status=%s)", CONTAINER_NAME, current)
    _docker_checked("start", CONTAINER_NAME)
    if not _host_port_published():
        _evict_squatters()
        _recreate_with_port_publish(db_count)


def stop() -> None:
    """Stop the shared Redis container (no-op if missing)."""
    if status() == "missing":
        return
    _docker_tolerant("stop", CONTAINER_NAME)


def flushdb(index: int, db_count: int = DEFAULT_DB_COUNT) -> None:
    """FLUSHDB on the given Redis DB index.

    Called when a ticket releases its slot so the next ticket to grab the
    slot starts with a clean cache/queue.
    """
    count = db_count
    if not 0 <= index < count:
        msg = f"redis db index {index} out of range 0..{count - 1}"
        raise ValueError(msg)
    if status() != "running":
        logger.debug("Skipping flushdb(%d): %s not running", index, CONTAINER_NAME)
        return
    _docker_tolerant("exec", CONTAINER_NAME, "redis-cli", "-n", str(index), "FLUSHDB")
