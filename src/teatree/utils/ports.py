import socket

from teatree.utils.run import run_allowed_to_fail

# Duplicated from teatree.core.models.types to avoid circular import
# through Django model registration.
type Ports = dict[str, int]


def find_free_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for a free ephemeral port on *host* and return it.

    Binds to port 0 (the kernel picks a free port), reads the assigned port, then
    releases the socket. There is an inherent race between release and re-bind, so
    the caller must bind promptly — used by the ttyd web-terminal launcher, which
    spawns ``ttyd --port <n>`` immediately.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


# Container-internal ports (fixed). Host ports are auto-mapped by Docker
# Compose when the override declares ``ports: ["<container_port>"]`` with
# no left side — Docker picks a free host port, and ``docker compose port``
# is the single source of truth for which one it picked.
CONTAINER_PORTS: dict[str, int] = {
    "backend": 8000,
    "frontend": 80,
    "postgres": 5432,
}

COMPOSE_SERVICE_MAP: dict[str, tuple[str, int]] = {
    "web": ("backend", 8000),
    "frontend": ("frontend", 80),
    "db": ("postgres", 5432),
}


def get_service_port(
    compose_project: str,
    service: str,
    container_port: int,
    *,
    compose_file: str = "",
) -> int | None:
    cmd = ["docker", "compose", "-p", compose_project]
    if compose_file:
        cmd.extend(["-f", compose_file])
    cmd.extend(["port", service, str(container_port)])

    result = run_allowed_to_fail(cmd, expected_codes=None)
    if result.returncode != 0:
        return None
    output = result.stdout.strip() if isinstance(result.stdout, str) else ""
    if ":" not in output:
        return None
    _, _, port_str = output.rpartition(":")
    return int(port_str) if port_str.isdigit() else None


def get_worktree_ports(
    compose_project: str,
    *,
    compose_file: str = "",
) -> Ports:
    ports: Ports = {}
    for service, (name, container_port) in COMPOSE_SERVICE_MAP.items():
        host_port = get_service_port(compose_project, service, container_port, compose_file=compose_file)
        if host_port is not None:
            ports[name] = host_port
    return ports
