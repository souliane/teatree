"""Process helpers for the shared pytest worker pool contract."""

import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest

from teatree.quality.pytest_resource_contract import INHERITED_LEASE_ENV, POOL_ROOT_ENV

REPO = Path(__file__).resolve().parents[2]
NODE = (
    f"{REPO}/tests/teatree_quality/test_pytest_resource_contract.py"
    "::test_auto_workers_are_parallel_but_bounded_on_a_healthy_box"
)


@dataclass(frozen=True)
class ProbeSettings:
    sleep: float = 0
    fail: bool = False
    worker_cap: str | None = None


DEFAULT_PROBE_SETTINGS = ProbeSettings()


@pytest.fixture
def real_pytest_root() -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="teatree-real-pytest-") as directory:
        yield Path(directory)


def standalone_pytest_env(root: Path) -> dict[str, str]:
    env = {**os.environ, POOL_ROOT_ENV: str(root)}
    for key in (
        INHERITED_LEASE_ENV,
        "DJANGO_SETTINGS_MODULE",
        "PYTEST_CURRENT_TEST",
        "CI_NODE_TOTAL",
        "CI_NODE_INDEX",
        "PYTEST_ADDOPTS",
        "PYTEST_XDIST_AUTO_NUM_WORKERS",
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        env.pop(key, None)
    return env


def pytest_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "--no-migrations", *args]


def make_probe(tmp_path: Path) -> Path:
    plugin = tmp_path / "pool_probe_plugin.py"
    plugin.write_text(
        "import os, time\n"
        "def pytest_configure(config):\n"
        "    if os.environ.get('PYTEST_XDIST_WORKER'):\n"
        "        with open(os.environ['POOL_PROBE_PIDS'], 'a') as stream:\n"
        "            stream.write(str(os.getpid()) + '\\n')\n"
        "def pytest_runtest_call(item):\n"
        "    time.sleep(float(os.environ.get('POOL_PROBE_SLEEP', '0')))\n"
        "    if os.environ.get('POOL_PROBE_FAIL') == '1':\n"
        "        raise AssertionError('probe failure')\n"
    )
    return plugin


def launch_pytest(
    tmp_path: Path,
    root: Path,
    label: str,
    *args: str,
    settings: ProbeSettings = DEFAULT_PROBE_SETTINGS,
) -> tuple[subprocess.Popen[str], Path, Path, Path]:
    make_probe(tmp_path)
    stdout_path = tmp_path / f"{label}.stdout"
    stderr_path = tmp_path / f"{label}.stderr"
    pids_path = tmp_path / f"{label}.pids"
    env = {
        **standalone_pytest_env(root),
        "POOL_PROBE_PIDS": str(pids_path),
        "POOL_PROBE_SLEEP": str(settings.sleep),
        "POOL_PROBE_FAIL": "1" if settings.fail else "0",
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    if settings.worker_cap is not None:
        env["PYTEST_XDIST_AUTO_NUM_WORKERS"] = settings.worker_cap
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        run = subprocess.Popen(
            [sys.executable, "-m", "pytest", "--no-migrations", "-p", "pool_probe_plugin", *args, "-vv", NODE],
            cwd=REPO,
            env=env,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
    return run, stdout_path, stderr_path, pids_path


def wait_for(predicate: Callable[[], bool], *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            message = "timed out waiting for real pytest process"
            raise AssertionError(message)
        time.sleep(0.05)


def worker_pids(path: Path) -> set[int]:
    return {int(pid) for pid in path.read_text(encoding="utf-8").splitlines()} if path.exists() else set()


def live_workers(paths: list[Path]) -> int:
    pids = set().union(*(worker_pids(path) for path in paths))
    live = 0
    for pid in pids:
        try:
            state = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()[2]
        except (OSError, IndexError):
            continue
        live += state != "Z"
    return live


def created_workers(stdout: str) -> int:
    matches = re.findall(r"created: (\d+)/(\d+) workers?", stdout)
    return int(matches[-1][1]) if matches else 0


def kill_group(run: subprocess.Popen[str]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(run.pid, signal.SIGKILL)
    run.wait(timeout=10)
