"""The shared ``_fail_open_or_deny`` router for the OVER-DENY gates (NEVER-LOCKOUT).

The over-deny gates (skill-loading, protect-default-branch, validate-mr
broken-env, block-uncovered-diff, agent-plan-gate, and the PRIVATE-surface
quote/banned downgrade) all route their deny through one helper:

- a self-rescue command (``t3 <overlay> gate disable``, ``db migrate``,
    ``t3 review gate fail-open enable``) is ALWAYS allowed — no gate may
    deny the very commands that rescue a lockout;
- with the master ``danger_gate_fail_open`` switch ON, every over-deny gate
    flips to fail-open (allow);
- otherwise the gate denies normally.

The helper fails CLOSED to enforcement: if the self-rescue / fail-open
resolution itself errors, it still denies — a broken import must never
silently relax a gate.
"""

import json
import sqlite3
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router


def _capture(data: dict, reason: str) -> tuple[bool, dict | None]:
    """Invoke the helper, capturing any deny JSON it writes to stdout."""
    buf = StringIO()
    with patch("sys.stdout", buf):
        blocked = router._fail_open_or_deny(data, reason)
    raw = buf.getvalue().strip()
    payload = json.loads(raw) if raw else None
    return blocked, payload


def _seed_config_db(path: Path, rows: dict[str, object]) -> None:
    """Seed the DB-home ``teatree_config_setting`` store the cold reader resolves."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        for key, value in rows.items():
            conn.execute(
                "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
                (key, json.dumps(value)),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "db.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return db


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


class TestDeniesByDefault:
    def test_denies_when_fail_open_off_and_not_self_rescue(self, config_db: Path) -> None:
        blocked, payload = _capture(_bash("git push origin main"), "BLOCKED: nope")
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
        assert "BLOCKED: nope" in payload["permissionDecisionReason"]

    def test_denies_for_a_non_bash_tool_call(self, config_db: Path) -> None:
        blocked, payload = _capture({"tool_name": "Edit", "tool_input": {"file_path": "/x"}}, "BLOCKED: edit")
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"


class TestFailOpenSwitch:
    def test_allows_everything_when_fail_open_enabled(self, config_db: Path) -> None:
        _seed_config_db(config_db, {"danger_gate_fail_open": True})
        blocked, payload = _capture(_bash("git push origin main"), "BLOCKED: nope")
        assert blocked is False
        assert payload is None

    def test_denies_when_fail_open_explicitly_false(self, config_db: Path) -> None:
        _seed_config_db(config_db, {"danger_gate_fail_open": False})
        blocked, _ = _capture(_bash("git push origin main"), "BLOCKED: nope")
        assert blocked is True


class TestSelfRescueAlwaysAllowed:
    @pytest.mark.parametrize(
        "command",
        [
            "t3 acme gate disable",
            "t3 acme gate skill-loading disable",
            "t3 review gate fail-open enable",
            "t3 acme db migrate",
            "python manage.py migrate",
        ],
    )
    def test_self_rescue_command_is_never_denied_even_with_fail_open_off(self, config_db: Path, command: str) -> None:
        # No config at all → fail-open is OFF → a normal command would be
        # denied. A self-rescue command must STILL be allowed.
        blocked, payload = _capture(_bash(command), "BLOCKED: nope")
        assert blocked is False, f"self-rescue command must never be denied: {command!r}"
        assert payload is None


class TestFailsClosedOnResolutionError:
    """A crash in the fail-open / self-rescue resolution still DENIES.

    The helper must never let a broken import or a raising resolver silently
    relax the gate — uncertainty errs toward enforcement here (the opposite
    of the gates' own broken-env posture, because this helper IS the relax
    path and must not relax by accident).
    """

    def test_resolver_exception_still_denies(self, config_db: Path) -> None:
        def _boom() -> bool:
            msg = "resolver blew up"
            raise RuntimeError(msg)

        with patch.object(router, "_danger_gate_fail_open_enabled", _boom):
            blocked, payload = _capture(_bash("git push origin main"), "BLOCKED: nope")
        assert blocked is True
        assert payload is not None
        assert payload["permissionDecision"] == "deny"
