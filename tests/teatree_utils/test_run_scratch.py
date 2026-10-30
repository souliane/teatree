import contextlib
import os
import select
import signal
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils.run_scratch import run_scratch_dir


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_failure_removes_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def fail() -> None:
        with run_scratch_dir("t3-probe-", keep_on_success=True) as root:
            (root / "payload").write_bytes(b"data")
            raise failure()

    with pytest.raises(failure):
        fail()

    assert not list(tmp_path.glob("t3-probe-*"))


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_signal_interrupts_and_restores_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signum: signal.Signals
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    previous = signal.getsignal(signum)

    def interrupt() -> None:
        with run_scratch_dir("t3-probe-") as root:
            (root / "payload").write_bytes(b"data")
            signal.raise_signal(signum)

    with pytest.raises(KeyboardInterrupt):
        interrupt()

    assert signal.getsignal(signum) == previous
    assert not list(tmp_path.glob("t3-probe-*"))


def test_success_is_kept_only_when_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with run_scratch_dir("t3-probe-") as removed:
        pass
    with run_scratch_dir("t3-probe-", keep_on_success=True) as kept:
        pass

    assert not removed.exists()
    assert kept.is_dir()
    assert list(tmp_path.glob("t3-probe-*")) == [kept]


def test_worker_thread_uses_cleanup_without_changing_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def worker() -> None:
        with run_scratch_dir("t3-probe-"):
            pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker).result(timeout=5)
    assert not list(tmp_path.glob("t3-probe-*"))


def test_cleanup_failure_warns_and_leaves_directory_for_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with (
        patch("teatree.utils.run_scratch.shutil.rmtree", side_effect=OSError("busy")),
        run_scratch_dir("t3-probe-") as root,
    ):
        pass

    assert root.is_dir()
    assert "run scratch cleanup failed" in caplog.text


def test_ignored_hangup_stays_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with run_scratch_dir("t3-probe-"):
            inside = signal.getsignal(signal.SIGHUP)
        after = signal.getsignal(signal.SIGHUP)
    finally:
        signal.signal(signal.SIGHUP, previous)

    assert inside == signal.SIG_IGN
    assert after == signal.SIG_IGN


def test_read_only_and_unlistable_subtrees_are_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    with run_scratch_dir("t3-probe-") as root:
        (root / "pkg" / "mod").mkdir(parents=True)
        (root / "pkg" / "mod" / "payload").write_bytes(b"data")
        (root / "sealed").mkdir()
        (root / "sealed" / "payload").write_bytes(b"data")
        (root / "pkg" / "mod").chmod(0o555)
        (root / "pkg").chmod(0o555)
        (root / "sealed").chmod(0o000)

    assert not list(tmp_path.glob("t3-probe-*"))


_WRITING_CHILD = 'echo $$ >&2; exec 2>/dev/null; while :; do echo x >> "$1/log"; sleep 0.02; done'

_LIVE_CHILD_PARENT = f"""
from teatree.utils.run import run_streamed
from teatree.utils.run_scratch import run_scratch_dir

with run_scratch_dir("t3-probe-") as root:
    print(root, flush=True)
    run_streamed(["sh", "-c", {_WRITING_CHILD!r}, "sh", str(root)], check=False)
"""


def test_sigterm_with_a_live_writing_child_removes_the_root_and_stops_the_child(tmp_path: Path) -> None:
    child = 0
    with subprocess.Popen(
        [sys.executable, "-c", _LIVE_CHILD_PARENT],
        env={**os.environ, "TMPDIR": str(tmp_path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as parent:
        try:
            assert parent.stderr is not None
            assert select.select([parent.stderr], [], [], 30)[0], "child never announced its pid"
            child = int(parent.stderr.readline())
            parent.terminate()
            parent.wait(timeout=30)
        finally:
            parent.kill()
            if child:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child, signal.SIGKILL)
                    pytest.fail("the child outlived the parent")

    assert not list(tmp_path.glob("t3-probe-*"))
