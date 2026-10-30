import platform
import socket
from pathlib import Path

from teatree.utils.run import run_allowed_to_fail

# Duplicated from teatree.core.models.types to avoid circular import
# through Django model registration.
type Ports = dict[str, int]

# Docker publishes the host under these names to containers. Docker Desktop provides
# both; a Linux daemon provides them only when the container was started with
# ``--add-host=host.docker.internal:host-gateway``, which is why neither may be
# assumed and the fallback below exists.
_HOST_ALIASES = ("host.docker.internal", "gateway.docker.internal")

# The default bridge gateway, which is the host on a stock Linux daemon that
# publishes no alias at all.
_DEFAULT_BRIDGE_GATEWAY = "172.17.0.1"


def running_in_container() -> bool:
    """Whether THIS process is executing inside a container.

    Deliberately not an OS check. The containerized CLI reports ``platform.system()
    == "Linux"`` whatever the host is, so branching on the OS answers "what kind of
    machine is this" when the question is "which side of the boundary am I on".
    """
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        # No /proc at all (macOS, Windows): not a Linux container.
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods"))


def host_published_port_host() -> str:
    """The hostname that reaches a port published on the Docker HOST from here.

    ``localhost`` when running natively, which is the common case and unchanged.
    Inside a container that same name is the container's own loopback -- nothing is
    listening there, and a caller gets ECONNREFUSED against a port that is demonstrably
    open on the host -- so the host has to be named explicitly.

    The alias is PROBED rather than inferred from the platform: Docker Desktop resolves
    ``host.docker.internal``, a stock Linux daemon resolves neither alias, and asking
    the resolver is the only answer that holds on both without special-casing either.
    """
    if not running_in_container():
        return "localhost"
    return _resolved_host_alias()


def _resolved_host_alias() -> str:
    """The first host alias this container's resolver answers, else the bridge gateway."""
    for alias in _HOST_ALIASES:
        try:
            socket.gethostbyname(alias)
        except OSError:
            continue
        return alias
    return _DEFAULT_BRIDGE_GATEWAY


def docker_host_address() -> str:
    """The address a container on this daemon reaches the host by.

    Natively the host OS is a sound proxy for the daemon's: Docker Desktop
    publishes an alias, a stock Linux daemon publishes none and the bridge
    gateway is the address.

    Inside a container that proxy breaks. ``platform.system()`` reports Linux
    whatever the host is, so it answers "what kind of machine is this" when the
    question is "which side of the boundary am I on" — and a containerized CLI on
    a macOS host handed out the bridge gateway, which nothing is listening on
    there. From inside, the daemon's own resolver is the authority, so the alias
    is PROBED rather than inferred, exactly as in :func:`host_published_port_host`.
    """
    if running_in_container():
        return _resolved_host_alias()
    return "host.docker.internal" if platform.system() in {"Darwin", "Windows"} else _DEFAULT_BRIDGE_GATEWAY


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
