"""A local HTTP/1.1 keep-alive server that counts accepted connections and can drop requests on demand."""

import json
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast


class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.connections = 0
        self.methods: list[str] = []
        self.paths: list[str] = []
        self.bodies: list[bytes] = []
        self.redirects: dict[str, str] = {}
        self.links: dict[str, str] = {}
        self.statuses: dict[str, int] = {}
        self.payloads: dict[str, object] = {}
        self.drop_when: Callable[[int], bool] = lambda _served: False
        self.delay_seconds = 0.0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        self.connections += 1
        return super().get_request()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.served = 0

    def _answer(self) -> None:
        server = cast("ForgeServer", self.server)
        server.bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        server.methods.append(self.command)
        server.paths.append(self.path)
        time.sleep(server.delay_seconds)
        if server.drop_when(self.served):
            self.close_connection = True
            return
        self.served += 1
        body = json.dumps(server.payloads.get(self.path, {"ok": True})).encode()
        if self.path in server.redirects:
            self.send_response(301)
            self.send_header("Location", server.redirects[self.path])
        else:
            self.send_response(server.statuses.get(self.path, 200))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if link := server.links.get(self.path):
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def log_message(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


@contextmanager
def forge_server() -> Generator[ForgeServer]:
    server = ForgeServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
