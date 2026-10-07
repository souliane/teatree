"""A local site with known breakage for the real-browser ``t3 browser`` lane."""

import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override

_HANG_S = 30.0


def _refused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class BrokenSite:
    def __init__(self) -> None:
        self.refused_url = f"http://127.0.0.1:{_refused_port()}/diag-missing"
        self._stopping = threading.Event()
        pages, stopping = self._pages(), self._stopping

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/never-loads.png":
                    stopping.wait(_HANG_S)
                body = pages.get(self.path)
                status = HTTPStatus.INTERNAL_SERVER_ERROR if self.path == "/down" else HTTPStatus.OK
                self.send_response(status if body else HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body or b"")

            @override
            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.broken_url = f"{self.origin}/broken"
        self.clean_url = f"{self.origin}/clean"
        self.down_url = f"{self.origin}/down"
        self.stalled_url = f"{self.origin}/stalled"
        self.not_found_url = f"{self.origin}/diag-404.png"

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()
        self._server.shutdown()
        self._server.server_close()

    def _pages(self) -> dict[str, bytes]:
        icon = '<link rel="icon" href="data:,">'
        broken = f"""<!doctype html><html><head><title>broken</title>{icon}</head><body>
<script>console.error('diag-boom'); fetch('{self.refused_url}').catch(() => {{}});</script>
<img src="/diag-404.png" alt="missing">
<button onclick="console.error('diag-clicked')">Go</button>
</body></html>"""
        clean = f"<!doctype html><html><head><title>clean</title>{icon}</head><body><p>ok</p></body></html>"
        down = f"<!doctype html><html><head><title>down</title>{icon}</head><body><p>failed</p></body></html>"
        stalled = f"""<!doctype html><html><head><title>stalled</title>{icon}</head><body>
<script>console.error('diag-stalled');</script>
<img src="/never-loads.png" alt="pending">
</body></html>"""
        return {
            "/broken": broken.encode(),
            "/clean": clean.encode(),
            "/down": down.encode(),
            "/stalled": stalled.encode(),
        }
