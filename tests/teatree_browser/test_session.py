import json
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.browser.session import BrowserSession, BrowserUnavailableError
from teatree.browser.state import SessionFiles
from teatree.utils.singleton import current_context
from tests._git_repo import make_git_repo


@pytest.fixture
def session(tmp_path: Path) -> BrowserSession:
    files = SessionFiles(tmp_path / "session")
    files.root.mkdir()
    return BrowserSession(files)


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


def _publish(session: BrowserSession, *, pid: int, cdp_url: str) -> None:
    session.files.pid.write_text(str(pid), encoding="utf-8")
    session.files.endpoint.write_text(json.dumps({"cdp_url": cdp_url}), encoding="utf-8")


def test_a_dead_keeper_is_not_a_session(session: BrowserSession) -> None:
    _publish(session, pid=_dead_pid(), cdp_url="http://127.0.0.1:9")

    assert session.cdp_url() is None


def test_a_live_process_named_by_a_pid_file_nobody_locks_is_not_a_session(session: BrowserSession) -> None:
    _publish(session, pid=os.getpid(), cdp_url="http://127.0.0.1:9222")

    with patch("teatree.browser.session.httpx.get") as answered:
        assert session.cdp_url() is None

    answered.assert_not_called()


def test_a_stale_session_is_relaunched_with_a_fresh_profile(session: BrowserSession) -> None:
    _publish(session, pid=_dead_pid(), cdp_url="http://127.0.0.1:9")
    (session.files.profile / "SingletonLock").parent.mkdir()
    (session.files.profile / "SingletonLock").write_text("stale", encoding="utf-8")
    launched: list[list[str]] = []

    def _launch(argv: list[str], **_kwargs: object) -> subprocess.Popen:
        launched.append(argv)
        _publish(session, pid=os.getpid(), cdp_url="http://127.0.0.1:9222")
        return subprocess.Popen([sys.executable, "-c", ""])

    with (
        patch("teatree.browser.session.spawn_session_leader", side_effect=_launch),
        patch.object(BrowserSession, "cdp_url", side_effect=[None, "http://127.0.0.1:9222"]),
    ):
        cdp_url = session._ensure_keeper()

    assert cdp_url == "http://127.0.0.1:9222"
    assert launched == [[launched[0][0], "-m", "teatree.browser.keeper", str(session.files.root)]]
    assert not session.files.profile.exists()


def test_sessions_are_keyed_by_the_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))

    first = BrowserSession.for_checkout(Path("/work/a")).files.root
    second = BrowserSession.for_checkout(Path("/work/b")).files.root

    assert first != second
    assert first.parent == second.parent == tmp_path / "browser-sessions"


def test_a_directory_inside_a_checkout_shares_the_checkouts_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    repo = make_git_repo(tmp_path / "repo").resolve()
    (repo / "src").mkdir()

    assert BrowserSession.for_directory(repo / "src").files.root == BrowserSession.for_checkout(repo).files.root


def test_a_directory_outside_git_is_its_own_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path / "data"))
    loose = tmp_path / "loose"
    loose.mkdir()

    assert BrowserSession.for_directory(loose).files.root == BrowserSession.for_checkout(loose.resolve()).files.root


@pytest.fixture
def unrelated_leader() -> Iterator[subprocess.Popen[bytes]]:
    """A live session-leader process that has nothing to do with any browser session."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    yield process
    process.kill()
    process.wait()


def _stale_record(pid: int, *, with_context: bool) -> str:
    context = json.dumps(current_context().as_json()) + "\n" if with_context else ""
    return f"{pid}\n{context}"


@pytest.mark.parametrize("with_context", [False, True])
def test_close_never_signals_a_live_process_a_stale_pid_file_names(
    session: BrowserSession,
    unrelated_leader: subprocess.Popen[bytes],
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_context: bool,
) -> None:
    monkeypatch.setattr("teatree.browser.session.KEEPER_STOP_TIMEOUT_S", 0.2)
    session.files.pid.write_text(_stale_record(unrelated_leader.pid, with_context=with_context), encoding="utf-8")

    assert session.close() is True

    assert unrelated_leader.poll() is None
    assert not session.files.root.exists()


@pytest.mark.parametrize("with_context", [False, True])
def test_open_never_signals_a_live_process_a_stale_pid_file_names(
    session: BrowserSession, unrelated_leader: subprocess.Popen[bytes], *, with_context: bool
) -> None:
    session.files.pid.write_text(_stale_record(unrelated_leader.pid, with_context=with_context), encoding="utf-8")

    def _launch(_argv: list[str], **_kwargs: object) -> subprocess.Popen:
        session.files.endpoint.write_text(json.dumps({"cdp_url": "http://127.0.0.1:9222"}), encoding="utf-8")
        return subprocess.Popen([sys.executable, "-c", ""])

    with (
        patch("teatree.browser.session.spawn_session_leader", side_effect=_launch),
        patch.object(BrowserSession, "cdp_url", side_effect=[None, "http://127.0.0.1:9222"]),
    ):
        session._ensure_keeper()

    assert unrelated_leader.poll() is None


_HOLD_KEEPER_LOCK = """
import sys, time
from pathlib import Path
from teatree.browser.state import KEEPER_LOCK
from teatree.utils.singleton import singleton
with singleton(KEEPER_LOCK, pid_path=Path(sys.argv[1])):
    time.sleep(120)
"""


@pytest.fixture
def lock_holder(session: BrowserSession) -> Iterator[subprocess.Popen[bytes]]:
    """A live process holding this session's keeper lock, deaf to the stop file — the escalation target."""
    process = subprocess.Popen([sys.executable, "-c", _HOLD_KEEPER_LOCK, str(session.files.pid)])
    deadline = time.monotonic() + 20
    while not session._keeper_running():
        assert time.monotonic() < deadline, "the holder never took the keeper lock"
        time.sleep(0.05)
    yield process
    process.kill()
    process.wait()


def test_a_live_keeper_whose_endpoint_does_not_answer_is_not_a_session(
    session: BrowserSession, lock_holder: subprocess.Popen[bytes]
) -> None:
    session.files.endpoint.write_text(json.dumps({"cdp_url": "http://127.0.0.1:9"}), encoding="utf-8")

    assert session.cdp_url() is None


def test_a_live_keeper_whose_endpoint_answers_is_the_session(
    session: BrowserSession, lock_holder: subprocess.Popen[bytes]
) -> None:
    session.files.endpoint.write_text(json.dumps({"cdp_url": "http://127.0.0.1:9222"}), encoding="utf-8")

    with patch("teatree.browser.session.httpx.get") as answered:
        assert session.cdp_url() == "http://127.0.0.1:9222"

    assert answered.call_args.args == ("http://127.0.0.1:9222/json/version",)


def test_close_terminates_the_process_that_holds_the_keeper_lock(
    session: BrowserSession, lock_holder: subprocess.Popen[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("teatree.browser.session.KEEPER_STOP_TIMEOUT_S", 0.2)

    assert session.close() is True

    assert lock_holder.wait(timeout=10) == -signal.SIGTERM
    assert not session.files.root.exists()


def test_close_refuses_to_signal_a_lock_holder_from_another_pid_namespace(
    session: BrowserSession, lock_holder: subprocess.Popen[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("teatree.browser.session.KEEPER_STOP_TIMEOUT_S", 0.2)
    elsewhere = {**current_context().as_json(), "pid_namespace": "pid:[1]"}
    session.files.pid.write_text(f"{lock_holder.pid}\n{json.dumps(elsewhere)}\n", encoding="utf-8")

    with pytest.raises(BrowserUnavailableError, match="cannot be resolved from this runtime"):
        session.close()

    assert lock_holder.poll() is None
    assert session.files.root.is_dir()


def test_a_launcher_that_fails_is_reported_at_once(session: BrowserSession) -> None:
    failed = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])

    with (
        patch("teatree.browser.session.spawn_session_leader", return_value=failed),
        pytest.raises(BrowserUnavailableError, match=r"could not start \(exit 3\)"),
    ):
        session._ensure_keeper()


def test_session_files_are_private_to_their_owner(tmp_path: Path) -> None:
    session = BrowserSession(SessionFiles(tmp_path / "state" / "session"))

    def _launch(_argv: list[str], **_kwargs: object) -> subprocess.Popen:
        session.files.endpoint.write_text(json.dumps({"cdp_url": "http://127.0.0.1:9222"}), encoding="utf-8")
        return subprocess.Popen([sys.executable, "-c", ""])

    with (
        patch("teatree.browser.session.spawn_session_leader", side_effect=_launch),
        patch.object(BrowserSession, "cdp_url", side_effect=[None, "http://127.0.0.1:9222"]),
    ):
        session._ensure_keeper()

    modes = {
        path.name: stat.S_IMODE(path.stat().st_mode) for path in (session.files.root, *session.files.root.iterdir())
    }
    assert modes.pop("session") == 0o700
    assert set(modes.values()) == {0o600}, modes
