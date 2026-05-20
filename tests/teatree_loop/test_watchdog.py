"""Tests for ``teatree.loop.watchdog`` — laptop always-on session.

Parser side (account/session discovery) is unit-tested with synthetic JSON
under ``tmp_path``. The ``launchd`` plist writer and process-scan side are
integration-tested by patching the ``subprocess`` boundary and a fake home
directory; production code paths are not invoked.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.loop import watchdog
from teatree.loop.watchdog import (
    AccountState,
    current_active_account,
    discover_loop_sessions,
    launch_agent_plist,
    launch_agent_plist_path,
)


def _write_active_account(home: Path, account_uuid: str, email: str = "user@example.com") -> None:
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "userID": "abcd",
                "oauthAccount": {
                    "accountUuid": account_uuid,
                    "emailAddress": email,
                    "organizationUuid": "org-1",
                },
            },
        ),
        encoding="utf-8",
    )


def _write_session_state(
    home: Path,
    *,
    session_id: str,
    pid: int,
    account_uuid: str | None = None,
) -> None:
    sessions_dir = home / ".claude" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": str(home),
        "kind": "interactive",
    }
    if account_uuid is not None:
        payload["accountUuid"] = account_uuid
    (sessions_dir / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_loop_pin(home: Path, *, session_id: str, account_uuid: str) -> None:
    pin = home / ".claude" / "teatree-loop-session.json"
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(
        json.dumps({"sessionId": session_id, "accountUuid": account_uuid}),
        encoding="utf-8",
    )


# ── current_active_account ───────────────────────────────────────────


class TestCurrentActiveAccount:
    def test_reads_account_from_claude_json(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-a", email="a@example.com")
        state = current_active_account(home=tmp_path)
        assert state == AccountState(account_uuid="uuid-a", email="a@example.com")

    def test_missing_claude_json_returns_none(self, tmp_path: Path) -> None:
        assert current_active_account(home=tmp_path) is None

    def test_missing_oauth_block_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")
        assert current_active_account(home=tmp_path) is None

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / ".claude.json").write_text("not json", encoding="utf-8")
        assert current_active_account(home=tmp_path) is None


# ── discover_loop_sessions ───────────────────────────────────────────


class TestDiscoverLoopSessions:
    def test_no_sessions_dir_returns_empty(self, tmp_path: Path) -> None:
        assert discover_loop_sessions(active_account_uuid="uuid-a", home=tmp_path) == []

    def test_session_running_under_active_account(self, tmp_path: Path) -> None:
        _write_loop_pin(tmp_path, session_id="sess-1", account_uuid="uuid-a")
        _write_session_state(tmp_path, session_id="sess-1", pid=os.getpid())
        results = discover_loop_sessions(active_account_uuid="uuid-a", home=tmp_path)
        assert len(results) == 1
        info = results[0]
        assert info.session_id == "sess-1"
        assert info.pid == os.getpid()
        assert info.account_uuid == "uuid-a"
        assert info.is_alive
        assert info.belongs_to_active_account

    def test_session_orphaned_after_account_switch(self, tmp_path: Path) -> None:
        _write_loop_pin(tmp_path, session_id="sess-1", account_uuid="uuid-a")
        _write_session_state(tmp_path, session_id="sess-1", pid=os.getpid())
        results = discover_loop_sessions(active_account_uuid="uuid-b", home=tmp_path)
        assert len(results) == 1
        assert results[0].account_uuid == "uuid-a"
        assert not results[0].belongs_to_active_account

    def test_dead_pid_marked_not_alive(self, tmp_path: Path) -> None:
        _write_loop_pin(tmp_path, session_id="sess-1", account_uuid="uuid-a")
        _write_session_state(tmp_path, session_id="sess-1", pid=999_999)
        results = discover_loop_sessions(active_account_uuid="uuid-a", home=tmp_path)
        assert len(results) == 1
        assert not results[0].is_alive

    def test_unpinned_session_has_no_account_uuid(self, tmp_path: Path) -> None:
        _write_session_state(tmp_path, session_id="sess-1", pid=os.getpid())
        results = discover_loop_sessions(active_account_uuid="uuid-a", home=tmp_path)
        # No pin file → session not recognised as a loop session.
        assert results == []

    def test_corrupt_session_file_skipped(self, tmp_path: Path) -> None:
        _write_loop_pin(tmp_path, session_id="sess-1", account_uuid="uuid-a")
        sessions_dir = tmp_path / ".claude" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "garbage.json").write_text("not json", encoding="utf-8")
        assert discover_loop_sessions(active_account_uuid="uuid-a", home=tmp_path) == []


# ── needs_respawn ────────────────────────────────────────────────────


class TestNeedsRespawn:
    def test_no_sessions_needs_respawn(self, tmp_path: Path) -> None:
        assert watchdog.needs_respawn(home=tmp_path) is True

    def test_healthy_active_account_session_does_not_respawn(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-a")
        _write_loop_pin(tmp_path, session_id="sess-1", account_uuid="uuid-a")
        _write_session_state(tmp_path, session_id="sess-1", pid=os.getpid())
        assert watchdog.needs_respawn(home=tmp_path) is False

    def test_orphan_after_account_switch_needs_respawn(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-b")
        _write_loop_pin(tmp_path, session_id="sess-1", account_uuid="uuid-a")
        _write_session_state(tmp_path, session_id="sess-1", pid=os.getpid())
        assert watchdog.needs_respawn(home=tmp_path) is True

    def test_dead_pid_needs_respawn(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-a")
        _write_loop_pin(tmp_path, session_id="sess-1", account_uuid="uuid-a")
        _write_session_state(tmp_path, session_id="sess-1", pid=999_999)
        assert watchdog.needs_respawn(home=tmp_path) is True


# ── plist generation ─────────────────────────────────────────────────


class TestLaunchAgentPlist:
    def test_plist_contains_required_keys(self) -> None:
        body = launch_agent_plist(label="com.adrien.teatree-loop", t3_bin="/usr/local/bin/t3")
        assert "<key>Label</key>" in body
        assert "<string>com.adrien.teatree-loop</string>" in body
        assert "<key>KeepAlive</key>" in body
        assert "<true/>" in body
        assert "<key>RunAtLoad</key>" in body
        assert "/usr/local/bin/t3" in body
        assert "spawn-headless" in body
        # zsh login shell wrapper so PATH is correct
        assert "/bin/zsh" in body

    def test_plist_path_under_library_launchagents(self, tmp_path: Path) -> None:
        path = launch_agent_plist_path(label="com.adrien.teatree-loop", home=tmp_path)
        assert path == tmp_path / "Library" / "LaunchAgents" / "com.adrien.teatree-loop.plist"


# ── install_watchdog ─────────────────────────────────────────────────


class TestInstallWatchdog:
    def test_install_writes_plist_and_loads(self, tmp_path: Path) -> None:
        calls: list[list[str]] = []

        def fake_launchctl(cmd: list[str], **_: object) -> object:
            calls.append(list(cmd))

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        with patch.object(watchdog, "run_allowed_to_fail", side_effect=fake_launchctl):
            path = watchdog.install_watchdog(
                home=tmp_path,
                label="com.test.teatree-loop",
                t3_bin="/usr/local/bin/t3",
            )

        assert path.exists()
        assert "com.test.teatree-loop" in path.read_text(encoding="utf-8")
        # launchctl load was attempted on the new plist
        assert any("launchctl" in c[0] and "load" in c for c in calls)

    def test_uninstall_removes_plist(self, tmp_path: Path) -> None:
        path = tmp_path / "Library" / "LaunchAgents" / "com.test.teatree-loop.plist"
        path.parent.mkdir(parents=True)
        path.write_text("<plist/>", encoding="utf-8")

        def fake_launchctl(cmd: list[str], **_: object) -> object:
            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        with patch.object(watchdog, "run_allowed_to_fail", side_effect=fake_launchctl):
            watchdog.uninstall_watchdog(home=tmp_path, label="com.test.teatree-loop")

        assert not path.exists()


# ── pin_session ──────────────────────────────────────────────────────


class TestPinSession:
    def test_pin_writes_active_account(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-a")
        watchdog.pin_session(session_id="sess-1", home=tmp_path)
        pin = json.loads((tmp_path / ".claude" / "teatree-loop-session.json").read_text(encoding="utf-8"))
        assert pin == {"sessionId": "sess-1", "accountUuid": "uuid-a"}

    def test_pin_with_no_active_account_raises(self, tmp_path: Path) -> None:
        with pytest.raises(watchdog.WatchdogError):
            watchdog.pin_session(session_id="sess-1", home=tmp_path)
