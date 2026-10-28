"""Forge REST calls reuse bounded connections through their public clients."""

import json
import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from teatree.backends.github import api as github_api
from teatree.backends.gitlab.http_client import GitLabHTTPClient
from teatree.utils.forge_http import client_for_url, close_forge_clients


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.connections = 0

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        self.connections += 1
        return super().get_request()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


@contextmanager
def _forge_server() -> Generator[tuple[_Server, str]]:
    server = _Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_gitlab_clients_reuse_a_bounded_host_pool() -> None:
    with _forge_server() as (server, base):
        for _ in range(12):
            client = GitLabHTTPClient(token="test", base_url=f"{base}/api/v4")
            assert client.get_json("projects/1") == {"ok": True}
        assert server.connections <= 8


def test_github_rest_calls_reuse_a_bounded_host_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    with _forge_server() as (server, base):
        monkeypatch.setattr(github_api, "_api_url", lambda endpoint: f"{base}/{endpoint}", raising=False)
        with patch.object(github_api, "run_checked", side_effect=AssertionError("gh api opened a subprocess")):
            for _ in range(12):
                assert github_api.gh_api_get("repos/example/repo", token="test") == {"ok": True}
        assert server.connections <= 8


def test_pool_is_keyed_by_full_origin_and_can_close() -> None:
    first = client_for_url("https://one.example/api/v4/issues")
    assert first is client_for_url("https://one.example/other")
    assert first is not client_for_url("https://two.example/api/v4/issues")

    close_forge_clients()

    assert first.is_closed
    assert client_for_url("https://one.example/api/v4/issues") is not first
