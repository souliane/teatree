"""Process-wide bounded HTTP connection pools keyed by a forge origin."""

import atexit
from threading import Lock

import httpx

_MAX_CONNECTIONS = 8
_clients: dict[str, httpx.Client] = {}
_lock = Lock()


def client_for_url(url: str) -> httpx.Client:
    origin = str(httpx.URL(url).copy_with(path="", query=None, fragment=None))
    with _lock:
        client = _clients.get(origin)
        if client is None:
            client = httpx.Client(
                limits=httpx.Limits(max_connections=_MAX_CONNECTIONS, max_keepalive_connections=_MAX_CONNECTIONS),
            )
            _clients[origin] = client
        return client


def close_forge_clients() -> None:
    with _lock:
        clients = tuple(_clients.values())
        _clients.clear()
    for client in clients:
        client.close()


atexit.register(close_forge_clients)
