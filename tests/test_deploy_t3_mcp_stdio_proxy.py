# test-path: cross-cutting — drives deploy/t3 as a real bash program around the stdio proxy.
"""``deploy/t3 mcp serve`` runs behind the host stdio proxy, and nothing else does."""

import contextlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import pytest
from _deploy_wrapper_paths import copy_wrapper

from teatree.mcp.serve_lifecycle import is_serve_command
from teatree.utils import mcp_stdio_proxy
from tests._process_liveness import is_gone_within
from tests.teatree_utils._mcp_proxy_children import FakeChild, request

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
SYSTEM_PATH = os.defpath.strip(os.pathsep)
WARNING = "will not survive a container restart"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bash", path=SYSTEM_PATH) is None, reason="needs a system bash"),
]


@dataclass
class Layout:
    entry: Path
    docker_log: Path
    python_log: Path
    env: dict[str, str]
    cwd: Path

    def docker_execs(self) -> list[str]:
        lines = self.docker_log.read_text(encoding="utf-8").splitlines() if self.docker_log.exists() else []
        return [line for line in lines if " exec " in f" {line} " and "mcp serve" in line]

    def python_calls(self) -> list[str]:
        return self.python_log.read_text(encoding="utf-8").splitlines() if self.python_log.exists() else []


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _docker_stub(log: Path, fake: FakeChild) -> str:
    server = " ".join(fake.argv)
    return f"""#!/bin/bash
printf '%s\\n' "$*" | tr '\\n' ' ' >>{log}
echo >>{log}
if [ "$1" = inspect ]; then printf '%s' "bind $PWD"; exit 0; fi
if [ "$1" = version ]; then echo 99; exit 0; fi
for arg in "$@"; do
    if [ "$arg" = ps ]; then echo 'c0ffee teatree-worker False'; exit 0; fi
    if [ "$arg" = config ]; then exit 0; fi
    if [ "$arg" = exec ] || [ "$arg" = run ]; then
        case " $* " in *" mcp serve"*) exec {server} ;; esac
        echo DISPATCHED
        exit 0
    fi
done
exit 0
"""


def build_layout(tmp_path: Path, fake: FakeChild, *, python_ok: bool = True, with_proxy: bool = True) -> Layout:
    checkout = tmp_path / "checkout"
    entry = checkout / "deploy" / "t3"
    entry.parent.mkdir(parents=True)
    copy_wrapper(WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    (entry.parent / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    if with_proxy:
        proxy = checkout / "src" / "teatree" / "utils" / "mcp_stdio_proxy.py"
        proxy.parent.mkdir(parents=True)
        shutil.copy2(mcp_stdio_proxy.__file__, proxy)

    stubs = tmp_path / "stub-bin"
    stubs.mkdir()
    docker_log = tmp_path / "docker.log"
    python_log = tmp_path / "python.log"
    _executable(stubs / "docker", _docker_stub(docker_log, fake))
    probe_answer = "" if python_ok else 'case "$*" in *version_info*) exit 1 ;; esac\n'
    # A wrapper that lost its marker would nest proxies without bound; this caps it at one level.
    nesting_guard = '[ -z "$SHIM_INSIDE_PROXY" ] || exit 99\nexport SHIM_INSIDE_PROXY=1\n'
    _executable(
        stubs / "python3",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >>{python_log}\n{nesting_guard}{probe_answer}exec {sys.executable} "$@"\n',
    )

    env = {k: v for k, v in os.environ.items() if not k.startswith(("GITLAB_", "GITHUB_", "T3_", "TEATREE_"))}
    env.update(
        {
            "PATH": f"{stubs}{os.pathsep}{SYSTEM_PATH}",
            "TEATREE_HOST_HOME": str(tmp_path / "home"),
            "TEATREE_UPDATE_WAIT_SECONDS": "0",
        }
    )
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    return Layout(entry, docker_log, python_log, env, cwd)


@contextlib.contextmanager
def serving(layout: Layout, fake: FakeChild, *argv: str) -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        ["/bin/bash", str(layout.entry), *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=layout.env,
        cwd=layout.cwd,
    )
    try:
        yield proc
    finally:
        for pid in [proc.pid, *(spawn["pid"] for spawn in fake.spawns())]:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        proc.wait(10)
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                stream.close()


def call(proc: subprocess.Popen[bytes], request_id: object) -> dict[str, Any]:
    to_server = proc.stdin
    from_server = proc.stdout
    assert to_server is not None
    assert from_server is not None
    return exchange(to_server, from_server, request_id)


def exchange(to_server: IO[bytes], from_server: IO[bytes], request_id: object) -> dict[str, Any]:
    to_server.write(json.dumps(request(request_id, "tools/call", {})).encode() + b"\n")
    to_server.flush()
    return json.loads(from_server.readline())


def process_args(pid: int) -> list[str]:
    return Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[:-1]


needs_proc = pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="reads argv from /proc")


class TestMcpServeRunsBehindTheProxy:
    @needs_proc
    def test_the_wrapper_execs_the_proxy_around_a_marked_copy_of_itself(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        layout = build_layout(tmp_path, fake)
        with serving(layout, fake, "mcp", "serve", "--read-only") as proc:
            call(proc, 1)

            argv = process_args(proc.pid)

        checkout = layout.entry.parents[1]
        assert argv[1:] == [
            "-I",
            "-S",
            str(checkout / "src" / "teatree" / "utils" / "mcp_stdio_proxy.py"),
            "env",
            "TEATREE_MCP_PROXY_CHILD=1",
            str(layout.entry),
            "mcp",
            "serve",
            "--read-only",
        ]
        assert len(layout.docker_execs()) == 1

    def test_a_killed_server_is_dispatched_again_through_the_wrapper(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        layout = build_layout(tmp_path, fake)
        with serving(layout, fake, "mcp", "serve") as proc:
            first = call(proc, 1)
            os.kill(first["result"]["pid"], signal.SIGKILL)
            deadline = time.monotonic() + 30
            while "error" in (served := call(proc, "retry")) and time.monotonic() < deadline:
                time.sleep(0.1)

        assert served["result"]["spawn"] == 1
        assert len(layout.docker_execs()) == 2

    @needs_proc
    def test_the_reaper_matches_the_docker_child_and_never_the_proxy(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        layout = build_layout(tmp_path, fake)
        with serving(layout, fake, "mcp", "serve") as proc:
            call(proc, 1)
            proxy_command = " ".join(process_args(proc.pid))

        docker_command = "docker " + layout.docker_execs()[0]
        assert not is_serve_command(proxy_command)
        assert is_serve_command(docker_command)


class TestNothingElseIsWrapped:
    @pytest.mark.parametrize("argv", [["info"], ["doctor", "check"], ["mcp", "--help"]])
    def test_other_commands_dispatch_without_the_proxy(self, tmp_path: Path, argv: list[str]) -> None:
        fake = FakeChild(tmp_path, [{}])
        layout = build_layout(tmp_path, fake)

        completed = subprocess.run(
            ["/bin/bash", str(layout.entry), *argv],
            capture_output=True,
            text=True,
            env=layout.env,
            cwd=layout.cwd,
            timeout=60,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert "DISPATCHED" in completed.stdout
        assert layout.python_calls() == []

    def test_a_marked_mcp_serve_dispatches_directly(self, tmp_path: Path) -> None:
        fake = FakeChild(tmp_path, [{}])
        layout = build_layout(tmp_path, fake)
        layout.env["TEATREE_MCP_PROXY_CHILD"] = "1"
        with serving(layout, fake, "mcp", "serve") as proc:
            served = call(proc, 1)

        assert served["result"]["spawn"] == 0
        assert layout.python_calls() == []


class TestWithoutTheProxy:
    @pytest.mark.parametrize(("python_ok", "with_proxy"), [(False, True), (True, False)])
    def test_mcp_serve_falls_back_to_the_direct_dispatch_and_says_so(
        self, tmp_path: Path, *, python_ok: bool, with_proxy: bool
    ) -> None:
        fake = FakeChild(tmp_path, [{}])
        layout = build_layout(tmp_path, fake, python_ok=python_ok, with_proxy=with_proxy)
        frame = json.dumps(request(1, "tools/call", {})).encode() + b"\n"

        completed = subprocess.run(
            ["/bin/bash", str(layout.entry), "mcp", "serve"],
            input=frame,
            capture_output=True,
            env=layout.env,
            cwd=layout.cwd,
            timeout=60,
            check=False,
        )

        assert json.loads(completed.stdout)["result"]["spawn"] == 0
        assert WARNING in completed.stderr.decode()
        assert all("mcp_stdio_proxy" not in call for call in layout.python_calls())
        assert all(is_gone_within(spawn["pid"], seconds=5) for spawn in fake.spawns())
