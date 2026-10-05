# test-path: cross-cutting — tests the session resource guard in tests/_session_resource_guard.py.
"""The session resource guard must fail the controller for worker leaks."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._session_resource_guard import pytest_testnodedown

_CORE = Path(__file__).resolve().parents[1]


_CLEAN_BODY = """
def test_resource_probe():
    assert True
"""

_LEAKING_THREAD_BODY = """
import threading


def test_resource_probe():
    threading.Thread(target=lambda: threading.Event().wait(10), name="leaky-probe").start()
"""

_PARKED_ASGIREF_WORKER_BODY = """
import asyncio

from asgiref.sync import sync_to_async


def test_resource_probe():
    assert asyncio.run(sync_to_async(int, thread_sensitive=True)("1")) == 1
"""

_BUSY_POOL_WORKER_BODY = """
import threading
import time
from concurrent.futures import ThreadPoolExecutor

POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="busy-pool")


def test_resource_probe():
    job = POOL.submit({job})
    deadline = time.monotonic() + 5
    while not job.running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert job.running()
"""


def _run_probe(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "test_resource_probe.py"
    probe.write_text(body, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(_CORE), env.get("PYTHONPATH", "")))
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist",
            "-p",
            "tests._session_resource_guard",
            "-n",
            "2",
            "-q",
            str(probe),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_worker_leak_fails_controller_and_names_thread(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, _LEAKING_THREAD_BODY)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "leaky-probe" in result.stdout + result.stderr


def test_clean_workers_exit_zero(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, _CLEAN_BODY)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_parked_pool_worker_does_not_block_exit_so_it_is_not_a_leak(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, _PARKED_ASGIREF_WORKER_BODY)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "job",
    ["lambda: threading.Event().wait(10)", "time.sleep, 10"],
    ids=["python-job", "c-callable-job"],
)
def test_a_pool_worker_still_running_a_job_is_a_leak(tmp_path: Path, job: str) -> None:
    result = _run_probe(tmp_path, _BUSY_POOL_WORKER_BODY.format(job=job))
    assert result.returncode != 0, result.stdout + result.stderr
    assert "busy-pool_0" in result.stdout + result.stderr


def test_guard_is_registered_from_conftest(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.get_plugin("session-resource-guard") is not None


def test_crashed_worker_without_output_does_not_hide_its_failure() -> None:
    pytest_testnodedown(SimpleNamespace())
