"""A loopback-only TCP forward from a fixed host port into a container's own loopback.

The admin gunicorn binds the CONTAINER's ``127.0.0.1``, and
:class:`~teatree.core.middleware.LocalAdminAutoLoginMiddleware` authenticates a request
only when its real peer is loopback — so neither obvious way to reach it from a host
browser works: a published bridge port NATs the source to the docker gateway, and a
same-host reverse proxy in front of the admin is forbidden outright (``deploy/README.md``
§ "Access & networking"). Each accepted connection is bridged through ``docker exec -i``
instead, which publishes nothing and proxies nothing, so the loopback trust model is
untouched.

TWO VENUES REACH THIS, and only one of them can bind a host port. A native ``t3 admin``
binds ``127.0.0.1`` on the host directly. The containerized CLI (``deploy/t3``) cannot —
its ``127.0.0.1`` is the container's own — so it runs the SAME bridge inside a one-off
forwarder container whose port is PUBLISHED to the host's loopback. The forwarder's own
listener binds every interface because a published mapping forwards to the container's
interface and never to its loopback; the host side of that mapping is pinned to
``127.0.0.1``, so the dashboard still never leaves the host. Both venues end at the same
``docker exec`` bridge, so the admin's peer is its own loopback either way.

:mod:`teatree.agents.terminal_launcher` has the identical reachability problem — its ttyd
binds the container loopback and is documented as reached "through the same SSH tunnel as
the admin", which a developer machine has no equivalent of — and can be pointed at this
module later. It is deliberately NOT refactored onto it here.
"""

import os
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from teatree.paths import data_dir_root
from teatree.utils.ports import running_in_container
from teatree.utils.run import TimeoutExpired, run_allowed_to_fail, spawn_byte_pipe

#: The one host port a forward ever binds. Fixed rather than ephemeral so a bookmark keeps
#: working, and clear of 8801/8802, which developer SSH tunnels conventionally take.
FORWARD_PORT = 8803

_LOOPBACK = "127.0.0.1"
#: The forwarder container's OWN listener interface. A published mapping delivers to the
#: container's interface, never to its loopback, so a loopback bind there would publish a
#: port nothing can reach. Confinement is the host side of the mapping, pinned to
#: :data:`_LOOPBACK`, not this bind.
_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 — inside the forwarder container only; the published mapping confines it to the host
_ADMIN_SERVICE = "teatree-admin"
_ADMIN_CONTAINER_PORT = 8000
_RELAY_CHUNK = 65536
_DOCKER_PROBE_TIMEOUT_SECONDS = 10.0
_FORWARDER_NAME = "teatree-admin-forward"
_FORWARDER_MEMORY = "64m"
_DOCKER_SOCKET = "/var/run/docker.sock"

# Dialled from INSIDE the container, so gunicorn's peer is a genuine loopback address.
_BRIDGE_SOURCE = """\
import socket, sys, threading
conn = socket.create_connection(("{host}", {port}))
def upstream():
    while True:
        chunk = sys.stdin.buffer.read1({size})
        if not chunk:
            break
        conn.sendall(chunk)
    conn.shutdown(socket.SHUT_WR)
threading.Thread(target=upstream, daemon=True).start()
while True:
    chunk = conn.recv({size})
    if not chunk:
        break
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
"""


# Runs INSIDE the published forwarder container, on the image's bare python3 — teatree
# itself is installed on a named volume this one-off does not mount, so stdlib only.
_FORWARDER_SOURCE = """\
import socket, subprocess, sys, threading
def relay(conn):
    child = subprocess.Popen(
        ["docker", "exec", "-i", {container!r}, "python3", "-c", {bridge!r}],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def upstream():
        try:
            while True:
                chunk = conn.recv({size})
                if not chunk:
                    break
                child.stdin.write(chunk)
                child.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                child.stdin.close()
            except OSError:
                pass
    threading.Thread(target=upstream, daemon=True).start()
    try:
        while True:
            chunk = child.stdout.read1({size})
            if not chunk:
                break
            conn.sendall(chunk)
    except OSError:
        pass
    finally:
        conn.close()
        child.terminate()
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(({bind!r}, {port}))
listener.listen()
while True:
    client, _peer = listener.accept()
    threading.Thread(target=relay, args=(client,), daemon=True).start()
"""


def _pump_socket_to_pipe(source: socket.socket, sink: IO[bytes]) -> None:
    with suppress(OSError, ValueError):
        while chunk := source.recv(_RELAY_CHUNK):
            sink.write(chunk)
            sink.flush()
    with suppress(OSError, ValueError):
        sink.close()


def _pump_pipe_to_socket(source: IO[bytes], sink: socket.socket) -> None:
    """Relay whatever the child has produced so far, never waiting for a full buffer."""
    with suppress(OSError, ValueError):
        descriptor = source.fileno()
        while chunk := os.read(descriptor, _RELAY_CHUNK):
            sink.sendall(chunk)


def _running_admin_container() -> str:
    """The id of the running admin container, or ``""`` when the stack is not up.

    Bounded and non-raising: an absent docker binary, an unresponsive daemon and a stopped
    stack must all degrade to an actionable message rather than stall the command.
    """
    try:
        result = run_allowed_to_fail(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.service={_ADMIN_SERVICE}",
                "--filter",
                "status=running",
                "--format",
                "{{.ID}}",
            ],
            expected_codes=None,
            timeout=_DOCKER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutExpired):
        return ""
    first, _, _ = result.stdout.strip().partition("\n")
    return first


@dataclass(frozen=True, slots=True)
class _OwnerMarker:
    """The pid file recording which teatree process owns the forward on *port*."""

    port: int

    @property
    def path(self) -> Path:
        return data_dir_root() / f"loopback-forward-{self.port}.pid"

    def held_by_live_process(self) -> bool:
        """Whether a live teatree process claimed this port — the only proof of ownership.

        The neighbouring ports carry developer SSH tunnels, so an unclaimed listener is
        someone else's until proven otherwise.
        """
        try:
            pid = int(self.path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    def claim(self) -> None:
        with suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(f"{os.getpid()}\n", encoding="utf-8")

    def release(self) -> None:
        with suppress(OSError):
            self.path.unlink(missing_ok=True)


class LoopbackForward:
    """A ``127.0.0.1``-only listener bridging each connection into *container*'s loopback."""

    def __init__(self, container: str, container_port: int, *, port: int = FORWARD_PORT) -> None:
        self._container = container
        self._container_port = container_port
        self._port = port
        self._marker = _OwnerMarker(port)
        self._listener: socket.socket | None = None

    @property
    def url(self) -> str:
        return f"http://{_LOOPBACK}:{self._port}"

    @property
    def address(self) -> tuple[str, int] | None:
        """The address actually bound, or ``None`` before :meth:`start`."""
        if self._listener is None:
            return None
        host, port = self._listener.getsockname()
        return str(host), int(port)

    def start(self) -> None:
        """Bind loopback and serve until :meth:`close`; raises ``OSError`` when the port is taken."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((_LOOPBACK, self._port))
            listener.listen()
        except OSError:
            listener.close()
            raise
        self._listener = listener
        self._marker.claim()
        threading.Thread(target=self._accept_forever, args=(listener,), daemon=True).start()

    def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        self._marker.release()

    def _accept_forever(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _peer = listener.accept()
            except OSError:
                return
            threading.Thread(target=self._bridge, args=(client,), daemon=True).start()

    def _bridge(self, client: socket.socket) -> None:
        pipe = spawn_byte_pipe(
            [
                "docker",
                "exec",
                "-i",
                self._container,
                "python3",
                "-c",
                _BRIDGE_SOURCE.format(host=_LOOPBACK, port=self._container_port, size=_RELAY_CHUNK),
            ]
        )
        threading.Thread(target=_pump_socket_to_pipe, args=(client, pipe.stdin), daemon=True).start()
        try:
            _pump_pipe_to_socket(pipe.stdout, client)
        finally:
            with suppress(OSError):
                client.close()
            with suppress(OSError, ValueError):
                pipe.process.terminate()


@dataclass(frozen=True, slots=True)
class ForwardResult:
    """The base URL a host browser can reach the admin at, or why it could not be reached."""

    url: str = ""
    error: str = ""
    forward: LoopbackForward | None = None

    def close(self) -> None:
        """Stop a forward THIS process started; a reused one stays with the process owning it."""
        if self.forward is not None:
            self.forward.close()


def _docker(*args: str) -> str:
    """The stdout of a bounded, non-raising ``docker`` call — ``""`` when it could not answer."""
    try:
        result = run_allowed_to_fail(["docker", *args], expected_codes=None, timeout=_DOCKER_PROBE_TIMEOUT_SECONDS)
    except (OSError, TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _container_image(container: str) -> str:
    """The image the admin runs, so the forwarder is guaranteed to have one present locally."""
    return _docker("inspect", "--format", "{{.Config.Image}}", container)


def _docker_socket_gid() -> int:
    """The socket's group AS THIS CONTAINER SEES IT — the only coordinate the new one shares.

    Read from the mounted node rather than branched on the host OS: the forwarder joins the
    same daemon through the same mount, so whatever grants access here grants it there.
    """
    try:
        return Path(_DOCKER_SOCKET).stat().st_gid
    except OSError:
        return 0


def _forwarder_name(port: int) -> str:
    """Port-scoped, so a different ``--port`` can never be served by a forwarder on the old one."""
    return f"{_FORWARDER_NAME}-{port}"


def _ensure_published_forwarder(container: str, *, port: int) -> ForwardResult:
    """Publish the admin on the HOST's ``127.0.0.1:port`` from inside a container.

    The published mapping is derived from *port* on both sides, so the flag and the
    mapping cannot disagree. Only the host side is pinned to loopback — that is what
    keeps the dashboard local; the forwarder's own bind must accept every interface
    for the mapping to deliver at all.
    """
    name = _forwarder_name(port)
    url = f"http://{_LOOPBACK}:{port}"
    if _docker("ps", "--filter", f"name=^{name}$", "--filter", "status=running", "--format", "{{.ID}}"):
        return ForwardResult(url=url)
    image = _container_image(container)
    if not image:
        return ForwardResult(error=f"cannot resolve the {_ADMIN_SERVICE} container's image to run the forwarder from")
    _docker("rm", "--force", name)
    bridge = _BRIDGE_SOURCE.format(host=_LOOPBACK, port=_ADMIN_CONTAINER_PORT, size=_RELAY_CHUNK)
    started = _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--publish",
        f"{_LOOPBACK}:{port}:{port}",
        "--volume",
        f"{_DOCKER_SOCKET}:{_DOCKER_SOCKET}",
        "--group-add",
        str(_docker_socket_gid()),
        "--memory",
        _FORWARDER_MEMORY,
        "--entrypoint",
        "python3",
        image,
        "-c",
        _FORWARDER_SOURCE.format(
            container=container, bridge=bridge, size=_RELAY_CHUNK, bind=_ALL_INTERFACES, port=port
        ),
    )
    if not started:
        return ForwardResult(error=f"could not start the published forwarder container {name}")
    return ForwardResult(url=url)


def ensure_admin_forward(*, port: int = FORWARD_PORT) -> ForwardResult:
    """Make the containerized admin reachable at ``127.0.0.1:<port>`` from a host browser.

    Reuses a forward another teatree process already owns, and refuses to reuse a listener
    it cannot prove is teatree's. Every failure returns a message rather than raising or
    blocking: the caller is about to open a browser, not to run the factory.

    A containerized caller cannot bind a host port at all, so it publishes one instead —
    the only venue difference; both end at the same ``docker exec`` bridge.
    """
    container = _running_admin_container()
    if not container:
        return ForwardResult(
            error=f"no running {_ADMIN_SERVICE} container — start the stack (deploy/deploy.sh) to reach it from here"
        )
    if running_in_container():
        return _ensure_published_forwarder(container, port=port)
    forward = LoopbackForward(container, _ADMIN_CONTAINER_PORT, port=port)
    try:
        forward.start()
    except OSError as exc:
        if _OwnerMarker(port).held_by_live_process():
            return ForwardResult(url=forward.url)
        return ForwardResult(error=f"{_LOOPBACK}:{port} is held by a listener teatree does not own ({exc})")
    return ForwardResult(url=forward.url, forward=forward)


__all__ = ["FORWARD_PORT", "ForwardResult", "LoopbackForward", "ensure_admin_forward"]
