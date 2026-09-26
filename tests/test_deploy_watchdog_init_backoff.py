# test-path: cross-cutting — drives deploy/watchdog.sh (no src mirror).
"""The watchdog stops re-running an init container that keeps failing.

``up -d --no-recreate`` restarts the SAME exited init container, so a failing init
used to be replayed every pass forever. The count is keyed on the container id:
a redeploy recreates init under a new id and the count starts over.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parents[1] / "deploy" / "watchdog.sh"
_BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="needs bash + jq (present in the deploy image and CI)",
)


def _write_docker_stub(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = compose ] || exit 0\n'
        "shift\n"
        'while [ "${1:-}" = -p ] || [ "${1:-}" = -f ]; do shift 2; done\n'
        'sub="${1:-}"; shift || true\n'
        'case "$sub" in\n'
        '  ps) cat "$STUB_INIT_PS_FILE" ;;\n'
        '  up) printf "%s\\n" "$*" >>"$STUB_UP_LOG" ;;\n'
        '  exec) printf "%s\\n" "$*" >>"$STUB_EXEC_LOG"; cat >/dev/null ;;\n'
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class _Stack:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.bin_dir = tmp_path / "bin"
        _write_docker_stub(self.bin_dir)
        self.up_log = tmp_path / "up.log"
        self.exec_log = tmp_path / "exec.log"
        self.ledger = tmp_path / "init-failures.state"
        self.init_ps = tmp_path / "init-ps.json"
        self.init("exited", 1, "init-a")

    def init(self, state: str, exit_code: int, container_id: str) -> None:
        self.init_ps.write_text(json.dumps({"ID": container_id, "State": state, "ExitCode": exit_code}) + "\n")

    def run_pass(self) -> subprocess.CompletedProcess[str]:
        harness = self.tmp_path / "harness.sh"
        harness.write_text(f'set -uo pipefail\nsource "{WATCHDOG}"\nrestart_down_services\n', encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["STUB_INIT_PS_FILE"] = str(self.init_ps)
        env["STUB_UP_LOG"] = str(self.up_log)
        env["STUB_EXEC_LOG"] = str(self.exec_log)
        env["TEATREE_WATCHDOG_INIT_FAILURE_STATE"] = str(self.ledger)
        env["TEATREE_WATCHDOG_UNDELIVERED_STATE"] = str(self.tmp_path / "undelivered.state")
        env["TEATREE_WATCHDOG_DEPLOY_LOCK"] = str(self.tmp_path / "no-deploy.lock")
        return subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)

    def up_calls(self) -> int:
        return len(self.up_log.read_text().splitlines()) if self.up_log.exists() else 0

    def backoff_pages(self) -> list[str]:
        lines = self.exec_log.read_text().splitlines() if self.exec_log.exists() else []
        return [line for line in lines if "watchdog:init-backoff:" in line]


def test_a_failing_init_is_replayed_twice_then_left_alone(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)

    ups = []
    for _ in range(4):
        stack.run_pass()
        ups.append(stack.up_calls())

    assert ups == [1, 2, 2, 2]


def test_the_backoff_is_announced_once_with_the_init_id(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)

    for _ in range(5):
        stack.run_pass()

    pages = stack.backoff_pages()
    assert len(pages) == 1
    assert "watchdog:init-backoff:init-a" in pages[0]


def test_a_recreated_init_container_resumes_the_restarts(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)
    for _ in range(3):
        stack.run_pass()
    assert stack.up_calls() == 2

    stack.init("exited", 1, "init-b")
    stack.run_pass()

    assert stack.up_calls() == 3


def test_a_successful_init_clears_the_ledger(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)
    for _ in range(3):
        stack.run_pass()
    assert stack.ledger.exists()

    stack.init("exited", 0, "init-a")
    stack.run_pass()

    assert not stack.ledger.exists()
    assert stack.up_calls() == 3


def test_a_running_init_is_left_to_the_normal_restart(tmp_path: Path) -> None:
    stack = _Stack(tmp_path)
    stack.init("running", 0, "init-a")

    for _ in range(4):
        stack.run_pass()

    assert stack.up_calls() == 4
    assert stack.backoff_pages() == []
