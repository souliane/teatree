"""The forge connection pool: bounded, replays a stale keep-alive once, retries a connect once, reports reuse."""

import logging
import socket
import threading
from contextlib import closing
from unittest.mock import patch

import httpx
import pytest

from teatree.utils.forge_http import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_SAME_ORIGIN_REDIRECTS,
    SUMMARY_INTERVAL_SECONDS,
    ForgeConnections,
)
from tests._forge_server import forge_server

_NO_HEADERS: dict[str, str] = {}


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_sessions_are_keyed_by_full_origin_and_can_close() -> None:
    connections = ForgeConnections(max_connections=4)
    first = connections.session_for("https://one.example/api/v4/issues")

    assert first is connections.session_for("https://one.example/other")
    assert first is not connections.session_for("https://two.example/api/v4/issues")

    connections.close()

    assert first is not connections.session_for("https://one.example/api/v4/issues")


def test_a_get_is_replayed_once_when_a_reused_connection_went_stale() -> None:
    with forge_server() as server, closing(ForgeConnections(max_connections=4)) as connections:
        session = connections.session_for(server.base)
        assert session.get(server.base, headers=_NO_HEADERS, timeout=5).status_code == 200

        server.drop_when = lambda served: served >= 1
        server.methods.clear()

        assert session.get(server.base, headers=_NO_HEADERS, timeout=5).status_code == 200
        assert server.methods == ["GET", "GET"]
        assert server.connections == 2


def test_a_post_is_never_replayed_after_a_stale_keepalive() -> None:
    with forge_server() as server, closing(ForgeConnections(max_connections=4)) as connections:
        session = connections.session_for(server.base)
        session.post(server.base, headers=_NO_HEADERS, json={}, timeout=5)

        server.drop_when = lambda served: served >= 1
        server.methods.clear()

        with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
            session.post(server.base, headers=_NO_HEADERS, json={}, timeout=5)

        assert server.methods == ["POST"]


def test_a_get_on_a_fresh_connection_is_not_replayed() -> None:
    with forge_server() as server, closing(ForgeConnections(max_connections=4)) as connections:
        server.drop_when = lambda _served: True
        session = connections.session_for(server.base)

        with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
            session.get(server.base, headers=_NO_HEADERS, timeout=5)

        assert server.methods == ["GET"]


def test_a_connect_error_is_retried_once_after_a_jittered_pause() -> None:
    pauses: list[float] = []
    url = f"http://127.0.0.1:{_closed_port()}/"

    with (
        closing(ForgeConnections(max_connections=4, sleep=pauses.append, jitter=lambda: 0.25)) as connections,
        pytest.raises(httpx.ConnectError),
    ):
        connections.session_for(url).post(url, headers=_NO_HEADERS, json={}, timeout=5)

    assert pauses == [0.25]


def test_concurrent_requests_never_open_more_connections_than_the_cap() -> None:
    with forge_server() as server, closing(ForgeConnections(max_connections=2)) as connections:
        server.delay_seconds = 0.2
        session = connections.session_for(server.base)
        workers = [
            threading.Thread(target=session.get, args=(server.base,), kwargs={"headers": _NO_HEADERS, "timeout": 10})
            for _ in range(6)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        assert server.connections <= 2


def test_each_request_logs_whether_it_opened_or_reused_a_connection(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="teatree.utils.forge_http")
    with forge_server() as server, closing(ForgeConnections(max_connections=4)) as connections:
        session = connections.session_for(server.base)
        session.get(server.base, headers=_NO_HEADERS, timeout=5)
        session.get(server.base, headers=_NO_HEADERS, timeout=5)

    assert [r.getMessage().split(": ")[1] for r in caplog.records if r.getMessage().startswith("forge pool 127")] == [
        "opened a new connection",
        "reused one",
    ]


def test_a_summary_of_requests_against_reused_connections_is_logged_once_a_minute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="teatree.utils.forge_http")
    now = [0.0]
    with forge_server() as server, closing(ForgeConnections(max_connections=4, clock=lambda: now[0])) as connections:
        session = connections.session_for(server.base)
        for _ in range(3):
            session.get(server.base, headers=_NO_HEADERS, timeout=5)
        assert not [r for r in caplog.records if "last minute" in r.getMessage()]

        now[0] = SUMMARY_INTERVAL_SECONDS + 1
        session.get(server.base, headers=_NO_HEADERS, timeout=5)

    assert [r.getMessage() for r in caplog.records if "last minute" in r.getMessage()] == [
        "forge pool: 4 requests, 3 on reused connections, in the last minute"
    ]


def test_a_get_follows_a_redirect_that_stays_on_the_same_origin() -> None:
    with forge_server() as server, closing(ForgeConnections(max_connections=4)) as connections:
        server.redirects = {"/old": "/new"}
        session = connections.session_for(server.base)

        response = session.get(f"{server.base}/old", headers=_NO_HEADERS, timeout=5)

        assert response.status_code == 200
        assert server.paths == ["/old", "/new"]


def test_a_get_never_follows_a_redirect_to_another_host() -> None:
    with forge_server() as server, closing(ForgeConnections(max_connections=4)) as connections:
        server.redirects = {"/old": f"http://127.0.0.2:{server.server_port}/steal"}
        session = connections.session_for(server.base)

        response = session.get(f"{server.base}/old", headers={"PRIVATE-TOKEN": "secret"}, timeout=5)

        assert response.status_code == 301
        assert server.paths == ["/old"]


def test_a_redirect_loop_stops_after_the_cap() -> None:
    with forge_server() as server, closing(ForgeConnections(max_connections=4)) as connections:
        server.redirects = {"/loop": "/loop"}
        session = connections.session_for(server.base)

        response = session.get(f"{server.base}/loop", headers=_NO_HEADERS, timeout=5)

        assert response.status_code == 301
        assert len(server.paths) == MAX_SAME_ORIGIN_REDIRECTS + 1


def test_a_write_is_never_redirected() -> None:
    with forge_server() as server, closing(ForgeConnections(max_connections=4)) as connections:
        server.redirects = {"/old": "/new"}
        session = connections.session_for(server.base)

        response = session.post(f"{server.base}/old", headers=_NO_HEADERS, json={}, timeout=5)

        assert response.status_code == 301
        assert server.paths == ["/old"]


@pytest.mark.parametrize(
    ("requested", "connect"),
    [(60.0, CONNECT_TIMEOUT_SECONDS), (2.0, 2.0)],
)
def test_the_connect_limit_survives_a_per_call_timeout(requested: float, connect: float) -> None:
    with closing(ForgeConnections(max_connections=4)) as connections:
        session = connections.session_for("https://forge.example/")
        with patch.object(httpx.Client, "get") as get:
            session.get("https://forge.example/x", headers=_NO_HEADERS, timeout=requested)

    assert get.call_args.kwargs["timeout"] == httpx.Timeout(requested, connect=connect)
