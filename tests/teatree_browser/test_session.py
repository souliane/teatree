import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.browser.session import BrowserSession
from teatree.browser.state import SessionFiles


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


def test_a_live_process_whose_endpoint_does_not_answer_is_not_a_session(session: BrowserSession) -> None:
    _publish(session, pid=os.getpid(), cdp_url="http://127.0.0.1:9")

    assert session.cdp_url() is None


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
