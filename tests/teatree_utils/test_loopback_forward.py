"""The host-side loopback forward that makes the containerized admin reachable.

The module shipped untested. Its whole reason to exist is a trust boundary —
``LocalAdminAutoLoginMiddleware`` authenticates a request only when its real peer
is loopback — so the bind address is a SECURITY property, not a detail: a forward
that grew a ``0.0.0.0`` bind would auto-login anyone who can route to the box.
That, plus the two ways it declines (a listener it cannot prove is teatree's, and
a stack that is not up), is what these cover.

Every socket here is EPHEMERAL (port 0, read back) and owned by the ``sockets``
fixture, which closes each one on teardown including the failure path. Reserving a
fixed port instead made these tests collide with each other under xdist and leak an
unclosed socket that failed an unrelated test in the same shard.

Docker is the one unstoppable external and is stubbed; the sockets and the pid
marker are real.
"""

import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils.loopback_forward import (
    _BRIDGE_SOURCE,
    _DOCKER_PROBE_TIMEOUT_SECONDS,
    _FORWARDER_SOURCE,
    ForwardResult,
    LoopbackForward,
    _running_admin_containers,
    ensure_admin_forward,
)
from teatree.utils.run import TimeoutExpired

_ADMIN_CONTAINER = "cafef00dbeef"
_CONTAINER_PORT = 8000


@pytest.fixture
def sockets() -> Iterator[Callable[[], socket.socket]]:
    """Hand out listening sockets on ephemeral loopback ports; close every one on teardown.

    Teardown runs even when the test raises, so a failing assertion can never leak
    the socket into another test in the same xdist worker.
    """
    opened: list[socket.socket] = []

    def occupy() -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        opened.append(listener)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        return listener

    try:
        yield occupy
    finally:
        for listener in opened:
            listener.close()


@pytest.fixture
def forwards() -> Iterator[Callable[[int], LoopbackForward]]:
    """Start forwards on an ephemeral port and close each one on teardown."""
    started: list[LoopbackForward] = []

    def start(port: int = 0) -> LoopbackForward:
        forward = LoopbackForward(_ADMIN_CONTAINER, _CONTAINER_PORT, port=port)
        started.append(forward)
        forward.start()
        return forward

    try:
        yield start
    finally:
        for forward in started:
            forward.close()


@pytest.fixture
def marker_home(tmp_path: Path) -> Iterator[Path]:
    """Point the owner-marker at a throwaway dir so a real box's marker never leaks in."""
    with patch("teatree.utils.loopback_forward.data_dir_root", return_value=tmp_path):
        yield tmp_path


@pytest.fixture
def stack_up() -> Iterator[None]:
    """A host (not a container) whose admin container is running."""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        terminal_port = reservation.getsockname()[1]
    with (
        patch("teatree.utils.loopback_forward.running_in_container", return_value=False),
        patch("teatree.utils.loopback_forward._running_admin_containers", return_value=[_ADMIN_CONTAINER]),
        patch("teatree.utils.loopback_forward.TERMINAL_PORT", terminal_port),
    ):
        yield


def _port_of(listener: socket.socket) -> int:
    return int(listener.getsockname()[1])


class TestTheForwardBindsLoopbackOnly:
    def test_the_bound_address_is_loopback(self, marker_home: Path, forwards: Callable[..., LoopbackForward]) -> None:
        # The trust boundary: gunicorn auto-logins a loopback peer, so a bind on
        # any other interface hands that auto-login to the network.
        address = forwards().address

        assert address is not None
        assert address[0] == "127.0.0.1"

    def test_the_advertised_url_is_loopback(self) -> None:
        # Pure string derivation — no socket, so it pins the address on every host.
        assert LoopbackForward(_ADMIN_CONTAINER, _CONTAINER_PORT, port=8803).url == "http://127.0.0.1:8803"

    def test_no_address_before_start(self) -> None:
        assert LoopbackForward(_ADMIN_CONTAINER, _CONTAINER_PORT, port=8803).address is None

    def test_close_releases_the_port_and_the_marker(
        self, marker_home: Path, forwards: Callable[..., LoopbackForward], sockets: Callable[[], socket.socket]
    ) -> None:
        forward = forwards()
        address = forward.address
        assert address is not None
        port = address[1]
        assert (marker_home / f"loopback-forward-{port}.pid").is_file() or (
            marker_home / "loopback-forward-0.pid"
        ).is_file()

        forward.close()

        assert forward.address is None
        assert not list(marker_home.glob("loopback-forward-*.pid"))
        # The port is genuinely free again: SO_REUSEADDR clears TIME_WAIT but NOT a
        # still-listening socket, so this bind succeeds only because close() worked.
        rebind = sockets()
        rebind.close()


class TestReuseVersusRefusal:
    """An occupied port is teatree's to reuse ONLY when a live process claimed it."""

    def test_reuses_a_port_a_live_teatree_process_claimed(
        self, marker_home: Path, stack_up: None, sockets: Callable[[], socket.socket]
    ) -> None:
        blocker = sockets()
        port = _port_of(blocker)
        (marker_home / f"loopback-forward-{port}.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")

        result = ensure_admin_forward(port=port)

        assert result.url == f"http://127.0.0.1:{port}"
        assert result.error == ""
        # Reused, not owned: closing must not stop the process that actually holds it.
        assert all(forward.address != ("127.0.0.1", port) for forward in result.forwards)
        result.close()

    def test_refuses_a_listener_no_marker_claims(
        self, marker_home: Path, stack_up: None, sockets: Callable[[], socket.socket]
    ) -> None:
        # 8801/8802 carry developer SSH tunnels, so an unclaimed listener on a
        # neighbouring port is someone else's until proven otherwise.
        port = _port_of(sockets())

        result = ensure_admin_forward(port=port)

        assert result.url == ""
        assert "does not own" in result.error
        assert str(port) in result.error

    def test_refuses_a_listener_whose_claiming_process_is_gone(
        self, marker_home: Path, stack_up: None, sockets: Callable[[], socket.socket]
    ) -> None:
        port = _port_of(sockets())
        (marker_home / f"loopback-forward-{port}.pid").write_text(f"{_dead_pid()}\n", encoding="utf-8")

        result = ensure_admin_forward(port=port)

        assert result.url == ""
        assert "does not own" in result.error

    def test_a_free_port_yields_an_owned_forward(self, marker_home: Path, stack_up: None) -> None:
        result = ensure_admin_forward(port=0)
        try:
            assert result.error == ""
            assert len(result.forwards) == 2
            assert all(forward.address is not None and forward.address[0] == "127.0.0.1" for forward in result.forwards)
        finally:
            result.close()
        assert all(forward.address is None for forward in result.forwards)

    def test_a_foreign_terminal_listener_reports_a_warning_but_keeps_admin(
        self, marker_home: Path, stack_up: None, sockets: Callable[[], socket.socket]
    ) -> None:
        blocker = sockets()
        terminal_port = _port_of(blocker)
        with patch("teatree.utils.loopback_forward.TERMINAL_PORT", terminal_port):
            result = ensure_admin_forward(port=0)
        try:
            assert result.url.startswith("http://127.0.0.1:")
            assert f"terminal port {terminal_port}" in result.error
            assert len(result.forwards) == 1
        finally:
            result.close()

    def test_native_dashboard_cannot_take_the_terminal_port(self, marker_home: Path, stack_up: None) -> None:
        with patch("teatree.utils.loopback_forward.TERMINAL_PORT", 8804):
            result = ensure_admin_forward(port=8804)
        assert result.url == ""
        assert "reserved" in result.error

    def test_native_terminal_route_reuses_a_claimed_listener(
        self, marker_home: Path, stack_up: None, sockets: Callable[[], socket.socket]
    ) -> None:
        blocker = sockets()
        terminal_port = _port_of(blocker)
        (marker_home / f"loopback-forward-{terminal_port}.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
        with patch("teatree.utils.loopback_forward.TERMINAL_PORT", terminal_port):
            result = ensure_admin_forward(port=0)
        try:
            assert result.error == ""
            assert len(result.forwards) == 1
            assert result.url.startswith("http://127.0.0.1:")
        finally:
            result.close()


def _dead_pid() -> int:
    """A pid no live process holds — so ``os.kill(pid, 0)`` raises for the marker read."""
    for candidate in range(999_000, 1_000_000):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    pytest.skip("no free pid to model a dead owner")
    return 0


class TestItDeclinesInsteadOfBlocking:
    """The caller is about to open a browser, not run the factory: never raise, never hang."""

    def test_a_stopped_stack_is_reported_not_raised(self, marker_home: Path) -> None:
        with (
            patch("teatree.utils.loopback_forward.running_in_container", return_value=False),
            patch("teatree.utils.loopback_forward._running_admin_containers", return_value=[]),
        ):
            result = ensure_admin_forward(port=0)

        assert result.url == ""
        assert "no running teatree-admin container" in result.error
        assert result.forwards == ()

    def test_a_stopped_stack_is_reported_from_inside_a_container_too(self, marker_home: Path) -> None:
        # The venue changes HOW the forward is made, never whether the stack is up.
        with (
            patch("teatree.utils.loopback_forward.running_in_container", return_value=True),
            patch("teatree.utils.loopback_forward._running_admin_containers", return_value=[]),
        ):
            result = ensure_admin_forward(port=0)

        assert result.url == ""
        assert "no running teatree-admin container" in result.error

    def test_the_docker_probe_is_bounded(self) -> None:
        with patch("teatree.utils.loopback_forward.run_allowed_to_fail") as run:
            run.return_value.stdout = ""
            _running_admin_containers()

        assert run.call_args.kwargs["timeout"] == _DOCKER_PROBE_TIMEOUT_SECONDS

    @pytest.mark.parametrize("failure", [TimeoutExpired("docker", _DOCKER_PROBE_TIMEOUT_SECONDS), OSError("no docker")])
    def test_an_unusable_docker_degrades_to_no_container(self, failure: Exception) -> None:
        # An unresponsive daemon and an absent binary must both read as "not up",
        # which the caller turns into a message rather than a traceback.
        with patch("teatree.utils.loopback_forward.run_allowed_to_fail", side_effect=failure):
            assert _running_admin_containers() == []

    def test_only_the_production_projects_admin_is_reached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A scratch stack's admin would serve another DB, and lend its image to the forwarder."""
        monkeypatch.setenv("TEATREE_COMPOSE_PROJECT", "rolled")
        with (
            patch("teatree.utils.compose_project.running_in_container", return_value=False),
            patch("teatree.utils.loopback_forward.run_allowed_to_fail") as run,
        ):
            run.return_value.stdout = ""
            _running_admin_containers()
        assert "label=com.docker.compose.project=rolled" in run.call_args.args[0]

    def test_two_admin_containers_return_an_error_naming_both_ids(self) -> None:
        with (
            patch("teatree.utils.loopback_forward.run_allowed_to_fail") as run,
            patch("teatree.utils.loopback_forward._ensure_published_forwarder") as publish,
            patch("teatree.utils.loopback_forward.running_in_container", return_value=True),
        ):
            run.return_value.stdout = f"{_ADMIN_CONTAINER}\nsecond-one\n"
            result = ensure_admin_forward()

        assert result.url == ""
        assert _ADMIN_CONTAINER in result.error
        assert "second-one" in result.error
        publish.assert_not_called()


class TestForwardResultClose:
    def test_closing_a_reused_result_leaves_the_owner_alone(self) -> None:
        # No forward attached => nothing of ours to stop; must not raise.
        ForwardResult(url="http://127.0.0.1:8803").close()


@pytest.fixture
def containerized_stack_up() -> Iterator[list[list[str]]]:
    with _containerized_stack(lambda _cmd: "") as calls:
        yield calls


@contextmanager
def _containerized_stack(ps_answer: Callable[[list[str]], str]) -> Generator[list[list[str]]]:
    calls: list[list[str]] = []

    def record(cmd: list[str], **_kwargs: object) -> object:
        calls.append(list(cmd))
        stdout = ps_answer(cmd) if cmd[1:2] == ["ps"] else "forwarder-id\n"
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    with (
        patch("teatree.utils.loopback_forward.running_in_container", return_value=True),
        patch("teatree.utils.loopback_forward._running_admin_containers", return_value=[_ADMIN_CONTAINER]),
        patch("teatree.utils.loopback_forward.run_allowed_to_fail", side_effect=record),
        patch("teatree.utils.loopback_forward._docker_socket_gid", return_value=0),
    ):
        yield calls


def _a_running_forwarder(label: str | None, *, terminal_route: bool = True) -> Callable[[list[str]], str]:
    def answer(cmd: list[str]) -> str:
        label_filters = [
            cmd[index + 1]
            for index, value in enumerate(cmd[:-1])
            if value == "--filter" and cmd[index + 1].startswith("label=")
        ]
        expected = [f"label={label}"]
        if terminal_route:
            expected.append("label=teatree.forward-ports=8803,8804")
        return "old-forwarder" if not label_filters or label_filters == expected else ""

    return answer


def _run_argv(calls: list[list[str]]) -> list[str]:
    for cmd in calls:
        if cmd[1:2] == ["run"]:
            return cmd
    pytest.fail(f"no `docker run` was issued; got {calls}")
    return []


class TestTheContainerizedVenuePublishesAHostPort:
    """The CLI's own ``127.0.0.1`` is the CONTAINER's, so it must publish one instead.

    This is the whole defect: a containerized caller used to be refused outright, and
    ``t3 dashboard`` then handed the operator ``http://127.0.0.1:8000`` — an address that
    resolves on the host to a loopback where nothing listens.
    """

    def test_it_yields_a_host_url_instead_of_refusing(self, containerized_stack_up: list[list[str]]) -> None:
        result = ensure_admin_forward(port=8803)

        assert result.error == ""
        assert result.url == "http://127.0.0.1:8803"

    def test_dashboard_cannot_take_the_reserved_terminal_port(self, containerized_stack_up: list[list[str]]) -> None:
        result = ensure_admin_forward(port=8804)
        assert result.url == ""
        assert "reserved" in result.error
        assert not any(call[1:2] == ["run"] for call in containerized_stack_up)

    def test_it_publishes_and_bridges_the_terminal_port(self, containerized_stack_up: list[list[str]]) -> None:
        ensure_admin_forward(port=8803)
        argv = _run_argv(containerized_stack_up)
        publish = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--publish"]
        assert publish == ["127.0.0.1:8803:8803", "127.0.0.1:8804:8804"]
        source = argv[-1]
        assert "8804" in source
        assert 'create_connection(("127.0.0.1", 8804))' in source

    def test_the_published_mapping_is_pinned_to_the_host_loopback(
        self, containerized_stack_up: list[list[str]]
    ) -> None:
        # `0.0.0.0:8803:8803` would put the single-operator dashboard on the network.
        ensure_admin_forward(port=8803)

        argv = _run_argv(containerized_stack_up)
        assert "--publish" in argv
        assert argv[argv.index("--publish") + 1] == "127.0.0.1:8803:8803"

    def test_the_published_mapping_follows_the_resolved_port(self, containerized_stack_up: list[list[str]]) -> None:
        # A mapping that disagrees with --port is the same unreachable-url bug moved.
        result = ensure_admin_forward(port=9111)

        argv = _run_argv(containerized_stack_up)
        assert argv[argv.index("--publish") + 1] == "127.0.0.1:9111:9111"
        assert result.url == "http://127.0.0.1:9111"

    def test_the_forwarders_own_listener_binds_every_interface(self, containerized_stack_up: list[list[str]]) -> None:
        # A published mapping delivers to the container's INTERFACE, never its loopback,
        # so a loopback bind here would publish a port nothing can reach.
        ensure_admin_forward(port=8803)

        source = _run_argv(containerized_stack_up)[-1]
        assert "listener.bind(('0.0.0.0', port))" in source
        assert "(8803," in source
        assert "(8804," in source

    def test_it_bridges_through_the_admins_own_loopback(self, containerized_stack_up: list[list[str]]) -> None:
        # The trust anchor: gunicorn must still see 127.0.0.1 as its peer, so the last
        # hop is `docker exec` into the admin, never a route to a published port.
        ensure_admin_forward(port=8803)

        source = _run_argv(containerized_stack_up)[-1]
        assert '"docker", "exec", "-i", ' in source
        assert _ADMIN_CONTAINER in source
        assert 'create_connection(("127.0.0.1", 8000))' in source

    def test_a_forwarder_name_is_port_scoped(self, containerized_stack_up: list[list[str]]) -> None:
        # One name for two ports would silently serve the old port under a new --port.
        ensure_admin_forward(port=9111)

        argv = _run_argv(containerized_stack_up)
        assert argv[argv.index("--name") + 1] == "teatree-admin-forward-9111"

    def test_a_running_forwarder_is_reused_not_restarted(self) -> None:
        with _containerized_stack(_a_running_forwarder(f"teatree.admin-container={_ADMIN_CONTAINER}")) as calls:
            result = ensure_admin_forward(port=8803)

        assert result.url == "http://127.0.0.1:8803"
        assert [call[1] for call in calls] == ["ps"]
        assert f"label=teatree.admin-container={_ADMIN_CONTAINER}" in calls[0]

    def test_a_forwarder_missing_the_terminal_route_is_replaced(self) -> None:
        with _containerized_stack(
            _a_running_forwarder(f"teatree.admin-container={_ADMIN_CONTAINER}", terminal_route=False)
        ) as calls:
            result = ensure_admin_forward(port=8803)
        assert result.url == "http://127.0.0.1:8803"
        assert [call[1] for call in calls] == ["ps", "inspect", "rm", "run"]

    @pytest.mark.parametrize(
        "label", [None, "teatree.admin-container=former-admin"], ids=["unlabelled", "former-admin"]
    )
    def test_a_forwarder_left_by_another_admin_is_replaced(self, label: str | None) -> None:
        with _containerized_stack(_a_running_forwarder(label)) as calls:
            result = ensure_admin_forward(port=8803)

        assert result.url == "http://127.0.0.1:8803"
        assert [call[1] for call in calls] == ["ps", "inspect", "rm", "run"]
        assert calls[2] == ["docker", "rm", "--force", "teatree-admin-forward-8803"]
        run = _run_argv(calls)
        assert run[run.index("--label") + 1] == f"teatree.admin-container={_ADMIN_CONTAINER}"

    def test_a_forwarder_that_will_not_start_is_reported_not_raised(self) -> None:
        with (
            patch("teatree.utils.loopback_forward.running_in_container", return_value=True),
            patch("teatree.utils.loopback_forward._running_admin_containers", return_value=[_ADMIN_CONTAINER]),
            patch("teatree.utils.loopback_forward._docker", side_effect=["", "an/image", "", ""]),
        ):
            result = ensure_admin_forward(port=8803)

        assert result.url == ""
        assert "could not start the published forwarder" in result.error


def test_published_terminal_route_relays_websocket_upgrade(tmp_path: Path) -> None:
    docker = tmp_path / "docker"
    docker.write_text('#!/bin/sh\nshift 3\nexec "$@"\n', encoding="utf-8")
    docker.chmod(0o755)
    with socket.socket() as target, socket.socket() as reservation:
        target.bind(("127.0.0.1", 0))
        target.listen()
        target.settimeout(5)
        reservation.bind(("127.0.0.1", 0))
        host_port = reservation.getsockname()[1]
        terminal_port = target.getsockname()[1]
        bridge = _BRIDGE_SOURCE.format(host="127.0.0.1", port=terminal_port, size=65536)
        source = _FORWARDER_SOURCE.format(
            container=_ADMIN_CONTAINER, routes=((host_port, bridge),), size=65536, bind="127.0.0.1"
        )
        reservation.close()
        request = []

        def serve() -> None:
            with target.accept()[0] as upstream:
                request.append(upstream.recv(4096))
                upstream.sendall(b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\n\r\n")

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        process = subprocess.Popen(
            [sys.executable, "-c", source],
            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    client = socket.create_connection(("127.0.0.1", host_port), timeout=1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            with client:
                client.settimeout(5)
                client.sendall(b"GET /secret/ws HTTP/1.1\r\nUpgrade: websocket\r\n\r\n")
                assert client.recv(4096).startswith(b"HTTP/1.1 101 Switching Protocols")
            server.join(timeout=5)
            assert request == [b"GET /secret/ws HTTP/1.1\r\nUpgrade: websocket\r\n\r\n"]
        finally:
            process.terminate()
            process.wait(timeout=5)
