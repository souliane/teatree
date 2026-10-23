"""The proxy run as deploy/t3 runs it: ``python3 -I -S mcp_stdio_proxy.py <server argv>``."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import pytest

from teatree.utils import mcp_stdio_proxy as proxy_module
from tests._process_liveness import is_gone_within
from tests.teatree_utils._mcp_proxy_children import SDK_SERVER, FakeChild, exchange, sdk_initialize, write_child

pytestmark = pytest.mark.integration

PROXY = Path(proxy_module.__file__)
MIDDLEMAN = "import subprocess, sys; p = subprocess.Popen(sys.argv[1:]); print(p.pid, flush=True); p.wait()"


def proxy_argv(server_argv: list[str]) -> list[str]:
    return [sys.executable, "-I", "-S", str(PROXY), *server_argv]


@contextlib.contextmanager
def running_proxy(fake: FakeChild) -> Generator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        proxy_argv(fake.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        yield proc
    finally:
        _reap_everything(proc, fake.family())


def _reap_everything(proc: subprocess.Popen[bytes], pids: list[int]) -> None:
    for pid in [proc.pid, *pids]:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    proc.wait(10)
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            stream.close()


class TestStopSignals:
    @pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
    def test_a_stop_signal_exits_zero_and_leaves_no_descendant(self, tmp_path: Path, signum: int) -> None:
        fake = FakeChild(tmp_path, [{"grandchild": True}])
        with running_proxy(fake) as proc:
            assert proc.stdin is not None
            assert proc.stdout is not None
            exchange(proc.stdin, proc.stdout, 1)

            proc.send_signal(signum)

            assert proc.wait(timeout=10) == 0
            assert all(is_gone_within(pid, seconds=5) for pid in fake.family())
            assert len(fake.spawns()) == 1

    def test_the_client_close_sequence_leaves_no_descendant(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"grandchild": True}])
        with running_proxy(fake) as proc:
            assert proc.stdin is not None
            assert proc.stdout is not None
            exchange(proc.stdin, proc.stdout, 1)

            proc.send_signal(signal.SIGINT)
            time.sleep(0.1)
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
            time.sleep(0.4)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

            assert all(is_gone_within(pid, seconds=5) for pid in fake.family())


class TestParentDeath:
    def test_a_proxy_whose_parent_was_killed_stops_and_leaves_no_descendant(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{"grandchild": True}])
        middleman = subprocess.Popen(
            [sys.executable, "-c", MIDDLEMAN, *proxy_argv(fake.argv)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert middleman.stdin is not None
        assert middleman.stdout is not None
        proxy_pid = int(middleman.stdout.readline())
        try:
            exchange(middleman.stdin, middleman.stdout, 1)

            middleman.kill()
            middleman.wait(10)

            assert is_gone_within(proxy_pid, seconds=10)
            assert all(is_gone_within(pid, seconds=5) for pid in fake.family())
        finally:
            _reap_everything(middleman, [proxy_pid, *fake.family()])


class TestPipedUse:
    def test_a_piped_frame_is_answered_and_the_script_exits_with_the_server_code(self, tmp_path: Path) -> None:
        server = [sys.executable, str(write_child(tmp_path, "sdk_server.py", SDK_SERVER))]
        frame = json.dumps(sdk_initialize(0)).encode() + b"\n"

        completed = subprocess.run(proxy_argv(server), input=frame, capture_output=True, timeout=60, check=False)

        assert completed.returncode == 0
        assert json.loads(completed.stdout)["result"]["serverInfo"]["name"] == "proxy-probe"

    def test_every_restart_is_one_named_stderr_line_and_no_frame_reaches_stderr(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        proc = subprocess.Popen(
            proxy_argv(fake.argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        try:
            first = exchange(proc.stdin, proc.stdout, "secret-request-id")
            os.kill(first["result"]["pid"], signal.SIGKILL)
            while exchange(proc.stdin, proc.stdout, "retry").get("error"):
                time.sleep(0.1)
            proc.stdin.close()
            stderr = proc.communicate(timeout=20)[1].decode()
        finally:
            _reap_everything(proc, fake.family())

        lines = stderr.splitlines()
        assert all(line.startswith("mcp-stdio-proxy: ") for line in lines)
        assert any("exited with code 137" in line for line in lines)
        assert sum("restarting the server" in line for line in lines) == 1
        assert "secret-request-id" not in stderr
