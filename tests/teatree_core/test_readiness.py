"""Tests for ``teatree.core.readiness`` — runtime readiness probes."""

import http.server
import socket
import sys
import threading
import time
from collections.abc import Iterator
from typing import ClassVar

import pytest

from teatree.core.readiness import (
    CommandProbeSpec,
    HTTPProbeSpec,
    Probe,
    ProbeResult,
    command_probe,
    http_probe,
    run_probes,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Handler(http.server.BaseHTTPRequestHandler):
    """Configurable handler. Status, body, and headers come from class attributes.

    Pytest captures the stderr access logging the parent emits; we don't override
    ``log_message`` here because matching the parent's ``format`` parameter name
    would shadow the builtin.
    """

    status: ClassVar[int] = 200
    body: ClassVar[bytes] = b"ok"
    extra_headers: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        self.send_response(self.status)
        for key, value in self.extra_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


@pytest.fixture
def http_server() -> Iterator[tuple[str, type[_Handler]]]:
    port = _free_port()
    handler_cls = type("H", (_Handler,), {})  # fresh subclass per test
    server = http.server.HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", handler_cls
    finally:
        server.shutdown()
        server.server_close()


class TestProbeResult:
    def test_format_pass(self) -> None:
        r = ProbeResult(name="x", passed=True, reason="ok")
        assert r.format() == "[OK] x — ok"

    def test_format_fail(self) -> None:
        r = ProbeResult(name="x", passed=False, reason="nope")
        assert r.format() == "[FAIL] x — nope"

    def test_format_no_reason(self) -> None:
        assert ProbeResult(name="x", passed=True).format() == "[OK] x"


class TestProbeWraping:
    def test_check_fn_exception_becomes_failed_result(self) -> None:
        def boom() -> ProbeResult:
            msg = "kaboom"
            raise RuntimeError(msg)

        probe = Probe(name="p", description="d", check_fn=boom)
        result = probe.check()
        assert result.passed is False
        assert "RuntimeError" in result.reason
        assert "kaboom" in result.reason


class TestHTTPProbe:
    def test_passes_on_expected_status(self, http_server: tuple[str, type[_Handler]]) -> None:
        url, _ = http_server
        probe = http_probe(name="ok", description="d", spec=HTTPProbeSpec(url=f"{url}/"))
        result = probe.check()
        assert result.passed is True
        assert result.evidence.startswith("GET ")

    def test_fails_on_unexpected_status(self, http_server: tuple[str, type[_Handler]]) -> None:
        url, handler = http_server
        handler.status = 503
        handler.body = b"down"
        probe = http_probe(name="up", description="d", spec=HTTPProbeSpec(url=f"{url}/"))
        result = probe.check()
        assert result.passed is False
        assert "status 503" in result.reason

    def test_fails_when_body_does_not_contain_expected(self, http_server: tuple[str, type[_Handler]]) -> None:
        url, handler = http_server
        handler.body = b"<html>login</html>"
        probe = http_probe(
            name="login-page",
            description="d",
            spec=HTTPProbeSpec(url=f"{url}/", body_contains="ADMIN PORTAL"),
        )
        result = probe.check()
        assert result.passed is False
        assert "body does not contain" in result.reason

    def test_passes_when_body_contains_expected(self, http_server: tuple[str, type[_Handler]]) -> None:
        url, handler = http_server
        handler.body = b"<html>ADMIN PORTAL login</html>"
        probe = http_probe(
            name="login-page",
            description="d",
            spec=HTTPProbeSpec(url=f"{url}/", body_contains="ADMIN PORTAL"),
        )
        assert probe.check().passed is True

    def test_fails_on_connection_refused(self) -> None:
        port = _free_port()  # nothing listening on it
        probe = http_probe(
            name="dead",
            description="d",
            spec=HTTPProbeSpec(url=f"http://127.0.0.1:{port}/", timeout_seconds=1.0),
        )
        result = probe.check()
        assert result.passed is False
        assert any(token in result.reason for token in ("ConnectError", "ConnectionRefused", "OSError"))

    def test_checks_response_header_value(self, http_server: tuple[str, type[_Handler]]) -> None:
        url, handler = http_server
        handler.extra_headers = {"Access-Control-Allow-Origin": "http://localhost:4200"}
        probe = http_probe(
            name="cors",
            description="d",
            spec=HTTPProbeSpec(
                url=f"{url}/",
                response_header_equals={"Access-Control-Allow-Origin": "http://localhost:4200"},
            ),
        )
        assert probe.check().passed is True

    def test_fails_when_response_header_mismatches(self, http_server: tuple[str, type[_Handler]]) -> None:
        url, handler = http_server
        handler.extra_headers = {"Access-Control-Allow-Origin": "*"}
        probe = http_probe(
            name="cors",
            description="d",
            spec=HTTPProbeSpec(
                url=f"{url}/",
                response_header_equals={"Access-Control-Allow-Origin": "http://localhost:4200"},
            ),
        )
        result = probe.check()
        assert result.passed is False
        assert "Access-Control-Allow-Origin" in result.reason


class TestCommandProbe:
    def test_passes_on_expected_exit_code(self) -> None:
        probe = command_probe(
            name="echo",
            description="d",
            spec=CommandProbeSpec(args=[sys.executable, "-c", "pass"]),
        )
        assert probe.check().passed is True

    def test_fails_on_nonzero_exit(self) -> None:
        probe = command_probe(
            name="bad",
            description="d",
            spec=CommandProbeSpec(args=[sys.executable, "-c", "import sys; sys.exit(2)"]),
        )
        result = probe.check()
        assert result.passed is False
        assert "exit 2" in result.reason

    def test_fails_when_stdout_does_not_contain(self) -> None:
        probe = command_probe(
            name="stdout",
            description="d",
            spec=CommandProbeSpec(
                args=[sys.executable, "-c", "print('hello')"],
                stdout_contains="WORLD",
            ),
        )
        result = probe.check()
        assert result.passed is False
        assert "stdout does not contain" in result.reason

    def test_passes_when_stdout_contains_expected(self) -> None:
        probe = command_probe(
            name="stdout",
            description="d",
            spec=CommandProbeSpec(
                args=[sys.executable, "-c", "print('hello WORLD')"],
                stdout_contains="WORLD",
            ),
        )
        assert probe.check().passed is True

    def test_fails_on_timeout(self) -> None:
        probe = command_probe(
            name="slow",
            description="d",
            spec=CommandProbeSpec(
                args=[sys.executable, "-c", "import time; time.sleep(5)"],
                timeout_seconds=0.2,
            ),
        )
        result = probe.check()
        assert result.passed is False
        assert "timed out" in result.reason

    def test_rejects_empty_args(self) -> None:
        probe = command_probe(name="empty", description="d", spec=CommandProbeSpec(args=[]))
        assert probe.check().passed is False


class TestRunProbes:
    def test_empty_list_returns_empty_list(self) -> None:
        assert run_probes([]) == []

    def test_runs_all_probes(self) -> None:
        probes = [
            Probe(name=f"p{i}", description="d", check_fn=lambda i=i: ProbeResult(name=f"p{i}", passed=True))
            for i in range(5)
        ]
        results = run_probes(probes)
        assert [r.name for r in results] == ["p0", "p1", "p2", "p3", "p4"]
        assert all(r.passed for r in results)

    def test_preserves_input_order_even_with_concurrency(self) -> None:
        # Build probes that complete out of submission order — first ones sleep longer.
        def make_probe(i: int, sleep_ms: int) -> Probe:
            def _check() -> ProbeResult:
                time.sleep(sleep_ms / 1000)
                return ProbeResult(name=f"p{i}", passed=True)

            return Probe(name=f"p{i}", description="d", check_fn=_check)

        probes = [make_probe(0, 100), make_probe(1, 50), make_probe(2, 10)]
        results = run_probes(probes)
        assert [r.name for r in results] == ["p0", "p1", "p2"]

    def test_collects_failures_alongside_passes(self) -> None:
        probes = [
            Probe(name="ok", description="d", check_fn=lambda: ProbeResult(name="ok", passed=True)),
            Probe(name="bad", description="d", check_fn=lambda: ProbeResult(name="bad", passed=False, reason="nope")),
        ]
        results = run_probes(probes)
        assert [r.passed for r in results] == [True, False]
