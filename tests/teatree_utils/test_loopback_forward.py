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
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils.loopback_forward import (
    _DOCKER_PROBE_TIMEOUT_SECONDS,
    ForwardResult,
    LoopbackForward,
    _running_admin_container,
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
    with (
        patch("teatree.utils.loopback_forward.running_in_container", return_value=False),
        patch("teatree.utils.loopback_forward._running_admin_container", return_value=_ADMIN_CONTAINER),
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
        assert result.forward is None

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
            assert result.forward is not None
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
            patch("teatree.utils.loopback_forward._running_admin_container", return_value=""),
        ):
            result = ensure_admin_forward(port=0)

        assert result.url == ""
        assert "no running teatree-admin container" in result.error
        assert result.forward is None

    def test_a_stopped_stack_is_reported_from_inside_a_container_too(self, marker_home: Path) -> None:
        # The venue changes HOW the forward is made, never whether the stack is up.
        with (
            patch("teatree.utils.loopback_forward.running_in_container", return_value=True),
            patch("teatree.utils.loopback_forward._running_admin_container", return_value=""),
        ):
            result = ensure_admin_forward(port=0)

        assert result.url == ""
        assert "no running teatree-admin container" in result.error

    def test_the_docker_probe_is_bounded(self) -> None:
        with patch("teatree.utils.loopback_forward.run_allowed_to_fail") as run:
            run.return_value.stdout = ""
            _running_admin_container()

        assert run.call_args.kwargs["timeout"] == _DOCKER_PROBE_TIMEOUT_SECONDS

    @pytest.mark.parametrize("failure", [TimeoutExpired("docker", _DOCKER_PROBE_TIMEOUT_SECONDS), OSError("no docker")])
    def test_an_unusable_docker_degrades_to_no_container(self, failure: Exception) -> None:
        # An unresponsive daemon and an absent binary must both read as "not up",
        # which the caller turns into a message rather than a traceback.
        with patch("teatree.utils.loopback_forward.run_allowed_to_fail", side_effect=failure):
            assert _running_admin_container() == ""

    def test_only_the_first_container_id_is_taken(self) -> None:
        with patch("teatree.utils.loopback_forward.run_allowed_to_fail") as run:
            run.return_value.stdout = f"{_ADMIN_CONTAINER}\nsecond-one\n"
            assert _running_admin_container() == _ADMIN_CONTAINER


class TestForwardResultClose:
    def test_closing_a_reused_result_leaves_the_owner_alone(self) -> None:
        # No forward attached => nothing of ours to stop; must not raise.
        ForwardResult(url="http://127.0.0.1:8803").close()


@pytest.fixture
def containerized_stack_up() -> Iterator[list[list[str]]]:
    """The containerized CLI venue with the stack up; every ``docker`` call is recorded.

    Docker is the one unstoppable external here — the forwarder is a real container on a
    real daemon — so the argv is what these assert against.
    """
    calls: list[list[str]] = []

    def record(cmd: list[str], **_kwargs: object) -> object:
        calls.append(list(cmd))
        # An empty `docker ps` (no forwarder yet) then a container id for `run`.
        stdout = "" if cmd[1:2] == ["ps"] else "forwarder-id\n"
        return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    with (
        patch("teatree.utils.loopback_forward.running_in_container", return_value=True),
        patch("teatree.utils.loopback_forward._running_admin_container", return_value=_ADMIN_CONTAINER),
        patch("teatree.utils.loopback_forward.run_allowed_to_fail", side_effect=record),
        patch("teatree.utils.loopback_forward._docker_socket_gid", return_value=0),
    ):
        yield calls


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
        assert "'0.0.0.0', 8803" in source

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
        with (
            patch("teatree.utils.loopback_forward.running_in_container", return_value=True),
            patch("teatree.utils.loopback_forward._running_admin_container", return_value=_ADMIN_CONTAINER),
            patch("teatree.utils.loopback_forward._docker", return_value="already-running") as docker,
        ):
            result = ensure_admin_forward(port=8803)

        assert result.url == "http://127.0.0.1:8803"
        assert [call.args[0] for call in docker.call_args_list] == ["ps"]

    def test_a_forwarder_that_will_not_start_is_reported_not_raised(self) -> None:
        with (
            patch("teatree.utils.loopback_forward.running_in_container", return_value=True),
            patch("teatree.utils.loopback_forward._running_admin_container", return_value=_ADMIN_CONTAINER),
            patch("teatree.utils.loopback_forward._docker", side_effect=["", "an/image", "", ""]),
        ):
            result = ensure_admin_forward(port=8803)

        assert result.url == ""
        assert "could not start the published forwarder" in result.error
