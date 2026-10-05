"""Fail a pytest session when its process leaves resources running."""

import gc
import multiprocessing
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest


def _parked_pool_workers() -> set[int]:
    # The interpreter's atexit hook wakes a pool worker idle in its queue; only a worker mid-job pins exit.
    return {
        ident
        for ident, frame in sys._current_frames().items()
        if frame.f_globals.get("__name__") == "concurrent.futures.thread" and frame.f_code.co_name == "_worker"
    }


def _live_resources() -> list[str]:
    parked = _parked_pool_workers()
    threads = [
        thread
        for thread in threading.enumerate()
        if thread is not threading.main_thread() and not thread.daemon and thread.ident not in parked
    ]
    children = [child for child in multiprocessing.active_children() if child.is_alive() and not child.daemon]
    popens = [obj for obj in gc.get_objects() if isinstance(obj, subprocess.Popen) and obj.poll() is None]
    leaks = [
        f"thread {thread.name} target={getattr(getattr(thread, '_target', None), '__qualname__', None)}"
        for thread in threads
    ]
    leaks.extend(f"process {child.name} pid={child.pid}" for child in children)
    for proc in popens:
        command = proc.args[0] if isinstance(proc.args, (list, tuple)) else proc.args
        leaks.append(f"process {Path(str(command)).name} pid={proc.pid}")
    return leaks


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    """Report this process's leaks and mark a worker failure for xdist."""
    config = session.config
    if not hasattr(config, "workerinput") and getattr(config.option, "numprocesses", 0):
        return
    leaks = _live_resources()
    if not leaks:
        return
    message = "pytest teardown leaked non-daemon resources: " + ", ".join(leaks)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if hasattr(config, "workeroutput"):
        workeroutput: dict[str, Any] = config.workeroutput
        workeroutput["shouldfail"] = message
        workeroutput["resource_leaks"] = message
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        sys.stderr.write(f"{message}\n")
        sys.stderr.flush()
    else:
        reporter.write_line(message, red=True)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any) -> None:
    """Name the leaking worker in controller output."""
    message = getattr(node, "workeroutput", {}).get("resource_leaks")
    if message:
        sys.stderr.write(f"[{node.gateway.id}] {message}\n")
        sys.stderr.flush()
