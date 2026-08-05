import hashlib
import platform
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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


# Stable per-worktree host-port window. Sits ABOVE the IANA registered range
# and BELOW the Linux default ephemeral range (``net.ipv4.ip_local_port_range``
# defaults to ``32768 60999``), so a stable assignment never collides with a
# port the kernel hands out to an unrelated ``bind(0)``. The window is stated
# relative to that ephemeral floor rather than hard-coded to a lore value.
STABLE_PORT_WINDOW_START = 20000
STABLE_PORT_WINDOW_END = 32767  # inclusive; one below the default ephemeral floor (32768)
_STABLE_PORT_WINDOW_SIZE = STABLE_PORT_WINDOW_END - STABLE_PORT_WINDOW_START + 1


def _port_is_bindable(port: int, host: str = "127.0.0.1") -> bool:
    """Return ``True`` when *port* can be bound on *host* right now.

    Uses ``SO_REUSEADDR`` so a port left in ``TIME_WAIT`` by a prior run still
    reads as available — the same relaxation Docker's own publish path uses.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def stable_host_port(
    identity: str,
    container_port: int,
    *,
    is_available: Callable[[int], bool] = _port_is_bindable,
) -> int:
    """A deterministic host port in a fixed window, stable across restarts.

    Hashes ``(identity, container_port)`` into
    ``[STABLE_PORT_WINDOW_START, STABLE_PORT_WINDOW_END]`` so a worktree keeps
    the SAME host port across a compose ``down``/``up`` — unlike Docker's
    auto-mapping, which rotates a fresh port each ``up`` and breaks anything
    outside the compose project that persisted the value (a bookmarked URL, a
    provision-time config file, a registered callback).

    On a conflict — a hash collision with a sibling, or an unrelated listener
    already on the port — it linear-probes forward, wrapping at the window edge,
    so the failure mode degrades to "a different stable port" rather than a
    ``compose up`` abort. When the whole window is occupied it falls back to the
    bare deterministic assignment.

    *identity* is an OPAQUE string — core does not need to know what names a
    worktree; the caller passes whatever it uses. *is_available* is injected so
    tests exercise the probe without binding real sockets.
    """
    digest = hashlib.blake2b(f"{identity}:{container_port}".encode(), digest_size=8).digest()
    base = STABLE_PORT_WINDOW_START + int.from_bytes(digest, "big") % _STABLE_PORT_WINDOW_SIZE
    for offset in range(_STABLE_PORT_WINDOW_SIZE):
        candidate = STABLE_PORT_WINDOW_START + (base - STABLE_PORT_WINDOW_START + offset) % _STABLE_PORT_WINDOW_SIZE
        if is_available(candidate):
            return candidate
    return base


@dataclass(frozen=True, slots=True)
class SharedNetworkHazard:
    """A service attached to a network shared across worktree compose projects."""

    service: str
    network: str

    def format(self) -> str:
        return (
            f"service {self.service!r} is attached to shared network {self.network!r} — "
            "on a network that spans worktrees a bare service name resolves ACROSS worktree "
            "boundaries, so one worktree's frontend can silently reach another's backend. "
            "Give each worktree a project-scoped network instead."
        )


# A parsed docker-compose fragment: an untyped nested mapping (string keys,
# arbitrary values — services/networks/scalars). The compose schema is too
# open-ended to model as a dataclass, so this named alias documents the shape
# at each ``cast`` where the YAML value is narrowed from ``object``.
type _ComposeMapping = dict[str, object]


def _shared_network_names(networks: object) -> set[str]:
    """Top-level network names that are NOT scoped to the compose project.

    A network declared ``external: true`` or pinned to a fixed top-level
    ``name:`` is shared by every compose project (worktree) that attaches to it;
    a plain project-scoped network is namespaced per project and is safe. Takes
    the raw parsed value and narrows internally — the parsed YAML is untyped, so
    every level is ``object`` until checked.
    """
    if not isinstance(networks, dict):
        return set()
    shared: set[str] = set()
    for name, spec in cast("_ComposeMapping", networks).items():
        if isinstance(spec, dict):
            spec_map = cast("_ComposeMapping", spec)
            if spec_map.get("external") or "name" in spec_map:
                shared.add(str(name))
    return shared


def _service_network_names(service: object) -> list[str]:
    """The networks a service attaches to (compose accepts a list OR a mapping)."""
    if not isinstance(service, dict):
        return []
    networks = cast("_ComposeMapping", service).get("networks")
    if isinstance(networks, (dict, list)):
        return [str(name) for name in networks]
    return []


def shared_network_hazards(compose: Mapping[str, object]) -> list[SharedNetworkHazard]:
    """Flag services attached to a network shared across worktrees.

    Given a parsed ``docker-compose`` override, returns one
    :class:`SharedNetworkHazard` per (service, shared-network) attachment. On a
    shared network Docker's embedded DNS resolves a bare service name across
    worktree boundaries — a cross-wired stack that *looks* up but serves the
    wrong backend. A pure function over the parsed mapping: the caller owns YAML
    loading, which keeps this testable and free of a YAML dependency here.
    """
    shared = _shared_network_names(compose.get("networks"))
    if not shared:
        return []
    services = compose.get("services")
    if not isinstance(services, dict):
        return []
    return [
        SharedNetworkHazard(service=str(name), network=net)
        for name, service in services.items()
        for net in _service_network_names(service)
        if net in shared
    ]


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
