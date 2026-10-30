import signal
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
