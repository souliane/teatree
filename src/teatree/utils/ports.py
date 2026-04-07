import fcntl
import socket
import subprocess
from pathlib import Path

# Duplicated from teatree.core.models.types to avoid circular import
# through Django model registration.
type Ports = dict[str, int]

# Container-internal ports (fixed). Only host ports vary per worktree.
CONTAINER_PORTS: dict[str, int] = {
    "backend": 8000,
    "frontend": 4200,
    "postgres": 5432,
    "redis": 6379,
}

# Default compose service → container port mapping.
COMPOSE_SERVICE_MAP: dict[str, tuple[str, int]] = {
    "web": ("backend", 8000),
    "frontend": ("frontend", 4200),
    "db": ("postgres", 5432),
    "rd": ("redis", 6379),
}


def port_in_use(port: int) -> bool:
    """Return True if *port* is already bound on localhost."""
    for family in (socket.AF_INET, socket.AF_INET6):
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind(("localhost", port))
        except OSError:
            return True
        finally:
            sock.close()
    return False


def find_free_port() -> int:
    """Return a single free port (OS-assigned on localhost)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _next_free_port(start: int, *, used: set[int]) -> int:
    """Walk from *start* until a free port is found."""
    port = start
    while port in used or port_in_use(port):
        used.add(port)
        port += 1
    return port


def find_free_ports(workspace_dir: str) -> Ports:
    """Find four free host ports for backend, frontend, postgres, redis.

    Uses a file lock in *workspace_dir* to prevent concurrent allocations
    from picking the same ports.  Port availability is checked via socket
    bind only — no file scanning.
    """
    lock_file = Path(workspace_dir) / ".port-allocation.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    handle = lock_file.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        used: set[int] = set()
        backend = _next_free_port(8001, used=used)
        used.add(backend)
        frontend = _next_free_port(4201, used=used)
        used.add(frontend)
        # Postgres: default 5432 for shared server, 5433+ for isolated
        postgres = _next_free_port(5432, used=used)
        used.add(postgres)
        redis = _next_free_port(6379, used=used)
        return {
            "backend": backend,
            "frontend": frontend,
            "postgres": postgres,
            "redis": redis,
        }
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def revalidate_ports(ports: Ports, workspace_dir: str) -> Ports:
    """Check allocated ports and replace any that became occupied.

    Returns a new ``Ports`` dict with the same keys. Ports that are still
    free are kept; occupied ports are replaced with fresh allocations.
    """
    lock_file = Path(workspace_dir) / ".port-allocation.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    handle = lock_file.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        used: set[int] = set(ports.values())
        result: Ports = {}
        for name, port in ports.items():
            if not port_in_use(port):
                result[name] = port
            else:
                start = CONTAINER_PORTS.get(name, port) + 1
                new_port = _next_free_port(start, used=used)
                used.add(new_port)
                result[name] = new_port
        return result
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


# ── Docker Compose port discovery ────────────────────────────────────


def get_service_port(
    compose_project: str,
    service: str,
    container_port: int,
    *,
    compose_file: str = "",
) -> int | None:
    """Ask docker-compose for the host port bound to *service:container_port*.

    Returns ``None`` if the service is not running or the port is not mapped.
    """
    cmd = ["docker", "compose", "-p", compose_project]
    if compose_file:
        cmd.extend(["-f", compose_file])
    cmd.extend(["port", service, str(container_port)])

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    # Output format: "0.0.0.0:8002\n" or ":::8002\n"
    output = result.stdout.strip()
    _, _, port_str = output.rpartition(":")
    return int(port_str) if port_str.isdigit() else None


def get_worktree_ports(
    compose_project: str,
    *,
    compose_file: str = "",
) -> Ports:
    """Query all compose services for their current host ports.

    Returns a dict like ``{"backend": 8002, "frontend": 4242, ...}``.
    Services that are not running are omitted.
    """
    ports: Ports = {}
    for service, (name, container_port) in COMPOSE_SERVICE_MAP.items():
        host_port = get_service_port(compose_project, service, container_port, compose_file=compose_file)
        if host_port is not None:
            ports[name] = host_port
    return ports


def free_port(port: int) -> int | None:
    """Kill the process holding *port* and return its PID, or ``None``."""
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415

    if not port_in_use(port):
        return None
    result = subprocess.run(
        ["lsof", "-ti", f":{port}"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
    if not pids:
        return None
    pid = pids[0]
    os.kill(pid, signal.SIGTERM)
    return pid
