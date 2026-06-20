"""Shared Redis container managed by teatree.

Teatree runs a single Redis container (`teatree-redis`) on localhost:6379.
Each ticket gets its own Redis DB index for isolation. Slot count is
`teatree.redis_db_count` in `~/.teatree.toml` (default 64). Slots are
allocated by `Ticket.objects.allocate_redis_slot()` and released (with
`FLUSHDB`) on teardown.

Redis's ``databases`` is a *fixed* per-container setting baked in at
``redis-server --databases N``; a running container created with a smaller
``N`` does NOT pick up a raised config. :func:`ensure_running` therefore
reconciles the live container's ``databases`` against the requested count and
recreates it when it falls short — without that, raising ``redis_db_count``
silently has no effect until the operator manually removes the container.
"""

import logging
import shutil
from subprocess import CompletedProcess

from teatree.utils.run import run_allowed_to_fail, run_checked

logger = logging.getLogger(__name__)

CONTAINER_NAME = "teatree-redis"
IMAGE = "redis:7-alpine"
HOST_PORT = 6379


class NativeRedisSquatterError(RuntimeError):
    """A non-Docker process holds host 6379, so ``teatree-redis`` cannot bind it."""


DEFAULT_DB_COUNT = 64

# `redis-cli CONFIG GET <key>` replies with two lines: the key, then its value.
_CONFIG_GET_REPLY_LINES = 2


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


def _configured_db_count() -> int | None:
    """Return the live container's ``databases`` setting, or ``None`` if unknown.

    ``redis-server --databases N`` fixes the number of selectable DBs for the
    container's whole lifetime — a config reload cannot raise it. ``CONFIG GET
    databases`` reports the value the running server was started with, so a
    container created by an older teatree (``--databases 16``) reports ``16``
    even after ``redis_db_count`` is raised. The probe returns ``None`` when the
    container is unreachable or the reply is unparsable, so the caller treats an
    inconclusive probe as "do not recreate" (fail-safe — never churn the shared
    container on a transient docker hiccup).
    """
    result = _docker_tolerant("exec", CONTAINER_NAME, "redis-cli", "CONFIG", "GET", "databases")
    if result.returncode != 0:
        return None
    # `redis-cli CONFIG GET databases` prints two lines: the key then the value.
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < _CONFIG_GET_REPLY_LINES:
        return None
    try:
        return int(lines[1])
    except ValueError:
        return None


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


def _native_host_listeners() -> list[str]:
    """Return ``pid/command`` strings for native (non-Docker) listeners on host 6379.

    ``_evict_squatters`` only handles *container* squatters. A native
    ``redis-server`` started by Homebrew/systemd binds the host port directly,
    so ``docker run -p 6379:6379`` fails with ``bind: address already in
    use`` and ``teatree-redis`` is left in ``Created`` state — every worktree's
    cache/broker traffic then 500s (#1373 sibling). ``lsof`` is the portable
    probe (macOS + Linux); Docker's own port forwarder (``com.docker`` /
    ``docker-pr`` / ``dockerd``) is excluded so the shared container's own
    publish isn't mistaken for a squatter.
    """
    if shutil.which("lsof") is None:
        return []
    result = run_allowed_to_fail(
        ["lsof", "-nP", f"-iTCP:{HOST_PORT}", "-sTCP:LISTEN", "-F", "pc"],
        expected_codes=None,
    )
    listeners: list[str] = []
    pid = ""
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            pid = line[1:].strip()
        elif line.startswith("c"):
            command = line[1:].strip()
            if command and not _is_docker_forwarder(command):
                listeners.append(f"{pid}/{command}")
    return listeners


def _is_docker_forwarder(command: str) -> bool:
    """True when *command* is Docker's own port-publish proxy, not a native squatter."""
    lowered = command.lower()
    return any(token in lowered for token in ("docker", "vpnkit", "com.docke"))


def _guard_native_squatter() -> None:
    """Raise an actionable error when a native process holds host 6379.

    Eviction is deliberately not attempted: a native ``redis-server`` is
    almost always a user-managed service (Homebrew/systemd) whose silent kill
    would surprise the user and lose data. Naming the ``pid/command`` lets the
    user stop it (``brew services stop redis`` / ``kill <pid>``) and re-run.
    """
    listeners = _native_host_listeners()
    if listeners:
        joined = ", ".join(listeners)
        msg = (
            f"A native (non-Docker) process is listening on host port {HOST_PORT} "
            f"[{joined}], so the shared '{CONTAINER_NAME}' container cannot bind it "
            "(docker run would fail with 'address already in use'). Stop the native "
            "service (e.g. `brew services stop redis` or `kill <pid>`) and re-run, or "
            "let teatree manage Redis exclusively."
        )
        raise NativeRedisSquatterError(msg)


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


def _recreate(db_count: int, reason: str) -> None:
    logger.info("%s %s — recreating to reconcile it", CONTAINER_NAME, reason)
    _docker_tolerant("stop", CONTAINER_NAME)
    _docker_tolerant("rm", CONTAINER_NAME)
    _evict_squatters()
    _guard_native_squatter()
    _create(db_count)


def _needs_recreate(db_count: int) -> str:
    """Reason a *running* container must be recreated, or ``""`` when it's fine.

    Two reconciliations share one recreate path: the missing ``-p 6379:6379``
    host publish (#1373) and a ``databases`` count below the requested pool. The
    db-count check is fail-safe — an inconclusive probe (``None``, docker hiccup)
    reports "no recreate" so a transient error never churns the shared container.
    """
    if not _host_port_published():
        return f"lacks the :{HOST_PORT} host publish"
    configured = _configured_db_count()
    if configured is not None and configured < db_count:
        return f"has only {configured} Redis DBs (want {db_count})"
    return ""


def ensure_running(db_count: int = DEFAULT_DB_COUNT) -> None:
    """Start the shared Redis container, self-healing publish and DB-count drift.

    Idempotent. A *running* container is reconciled (recreated) when it lacks the
    ``-p 6379:6379`` publish (#1373) OR was started with fewer ``databases`` than
    the requested pool — Redis bakes ``--databases N`` in at boot, so a container
    created by an older teatree (``--databases 16``) never picks up a raised
    ``redis_db_count`` and slot allocation keeps failing with
    ``RedisSlotsExhaustedError`` until the operator manually removes it. A
    non-``teatree-redis`` container squatting on host port 6379 is evicted before
    any create/recreate so ``docker run -p 6379:6379`` doesn't fail with
    ``address already in use``. A *native* (non-Docker) ``redis-server`` holding
    the port cannot be safely evicted, so it raises
    :class:`NativeRedisSquatterError` naming the process instead of letting
    ``docker run`` fail with a cryptic bind error.
    """
    current = status()
    if current == "running":
        reason = _needs_recreate(db_count)
        if reason:
            _recreate(db_count, reason)
        return
    if current == "missing":
        _evict_squatters()
        _guard_native_squatter()
        _create(db_count)
        return
    logger.info("Starting existing %s container (status=%s)", CONTAINER_NAME, current)
    _docker_checked("start", CONTAINER_NAME)
    reason = _needs_recreate(db_count)
    if reason:
        _recreate(db_count, reason)


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
