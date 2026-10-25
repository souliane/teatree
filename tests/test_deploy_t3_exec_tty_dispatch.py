# test-path: cross-cutting — executes the real deploy/t3 against a stubbed Compose project.
"""`deploy/t3` allocates a container pty only when BOTH host stdin and stdout are terminals.

A captured stdout must stay a pipe inside the container, or `TOKEN="$(t3 <overlay> secrets get NAME)"`
typed in an interactive shell reads the container's tty and is refused (or gains a carriage return).
"""

import os
import pty
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_BASH = shutil.which("bash") or "/bin/bash"
_STUB = (
    "#!/bin/sh\n"
    'printf \'%s\\n\' "$*" >>"$DOCKER_CALLS"\n'
    'case "$1" in\n'
    "ps) printf 'worker teatree-worker False\\n' ;;\n"
    "compose|version|inspect) exit 0 ;;\n"
    "esac\n"
)


def _exec_flags(tmp_path: Path, *, stdin_tty: bool, stdout_tty: bool) -> list[str]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    docker = stub_dir / "docker"
    docker.write_text(_STUB, encoding="utf-8")
    docker.chmod(0o755)
    calls = tmp_path / "docker.calls"
    env = {key: value for key, value in os.environ.items() if not key.startswith("TEATREE_")}
    env.update(PATH=f"{stub_dir}:{env['PATH']}", HOME=str(tmp_path), DOCKER_CALLS=str(calls))
    terminals = [pty.openpty() for _ in range(2)]
    try:
        subprocess.run(
            [_BASH, str(WRAPPER), "teatree", "info"],
            env=env,
            cwd=tmp_path,
            stdin=terminals[0][1] if stdin_tty else subprocess.DEVNULL,
            stdout=terminals[1][1] if stdout_tty else subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=60,
        )
    finally:
        for master, slave in terminals:
            os.close(master)
            os.close(slave)
    (exec_call,) = [line for line in calls.read_text(encoding="utf-8").splitlines() if " exec " in line]
    return exec_call.split()


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty", "expects_no_tty_flag"),
    [(True, True, False), (True, False, True), (False, True, True), (False, False, True)],
    ids=["interactive", "stdout-captured", "stdin-piped", "fully-piped"],
)
def test_container_pty_follows_both_host_streams(
    tmp_path: Path, *, stdin_tty: bool, stdout_tty: bool, expects_no_tty_flag: bool
) -> None:
    assert ("-T" in _exec_flags(tmp_path, stdin_tty=stdin_tty, stdout_tty=stdout_tty)) is expects_no_tty_flag
