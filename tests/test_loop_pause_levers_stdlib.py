"""The Stop self-pump pause levers read durable state WITHOUT django.setup() (#2559, fast-hooks).

The Stop hook is invoked as a bare ``python3`` (``hooks.json``): the harness
never sources the user's shell profile and the interpreter is whatever the
harness picks — it has NO ``uv`` env, so an in-process ``django.setup()`` cannot
be relied on. Before #2559 both durable pause levers gated their read on that
bootstrap and FAILED OPEN when it failed — a durable DB pause / away override was
silently ineffective at suppressing the self-pump.

#2559 fixed that by shelling out to the ``t3`` CLI (a child process that
bootstraps Django). fast-hooks removes even that: the ``t3`` child cold-booted
Django (~3s), which — twice per Stop — dominated the ~15s Stop hook and blew the
30s timeout (the recurring TIMEOUT). Both levers now read durable state DIRECTLY
in stdlib: ``db_loop_state_suppresses_self_pump`` reads the ``teatree_loop_state``
row via the Django-free ``teatree.config.cold_reader.loop_status``, and
``_resolved_away_mode`` reads the manual-override JSON file + the no-schedule
default in pure stdlib (only a configured cron schedule, which needs ``croniter``,
still delegates to the ``t3`` subprocess).

These tests reproduce the bare-``python3`` context (the in-process bootstrap is
forced to fail) and prove a durable pause / away override STILL suppresses the
pump — now with no ``django.setup()`` AND no per-lever subprocess. The #2559
structural invariant (no in-process django bootstrap in the levers) is re-pinned
against the new mechanism.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _OWNER_LOOP, _write_loop_registry, handle_loop_self_pump

# ``hook_router`` puts its own dir on ``sys.path`` and binds the lever functions
# via bare ``from <sibling> import …`` — SEPARATE module instances from
# ``hooks.scripts.<sibling>`` (the dual identity in ``hooks/CLAUDE.md``). Read the
# instances the router actually calls.
gate = sys.modules["loop_state_self_pump_gate"]
away_probe = sys.modules["availability_away_probe"]


@pytest.fixture(autouse=True)
def _bare_python3_stop_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the real bare-``python3`` Stop hook context, fully isolated.

    The in-process django bootstrap is forced to fail (as in production), and the
    state dir / registry / bash-env / availability-schedule config are redirected
    into ``tmp_path`` so a developer's real config never leaks into the test. An
    EMPTY ``TEATREE_TOML`` means "no schedule windows", so the away probe never
    falls back to the ``t3`` subprocess unless a test configures a window.
    """
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "no-bash-env"))
    (tmp_path / "teatree.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("TEATREE_TOML", str(tmp_path / "teatree.toml"))
    # The router still imports the in-process bootstrap for OTHER handlers; force
    # it False to prove the levers do NOT depend on an in-process django.setup().
    monkeypatch.setattr(router, "bootstrap_teatree_django", lambda: False)


def _own_loop(session_id: str) -> None:
    _write_loop_registry(
        {
            _OWNER_LOOP: {
                "session_id": session_id,
                "agent_id": "a",
                "pid": os.getpid(),
                "heartbeat_ts": int(time.time()),
            }
        }
    )


def _fake_pending(monkeypatch: pytest.MonkeyPatch, entries: list[dict]) -> None:
    monkeypatch.setattr(router, "_consolidated_pending_work", lambda: entries)


def _config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, dispatch_status: str | None = None) -> None:
    """Build the PRIMARY DB (with a ``teatree_loop_state`` table) and point cold_reader at it.

    ``T3_CONFIG_DB`` makes ``cold_reader.canonical_config_db`` resolve this DB;
    its PARENT is the PRIMARY data dir the away probe reads
    ``availability_override.json`` from. *dispatch_status* seeds the ``dispatch``
    row; ``None`` leaves it absent (the ``enabled`` fall-through).
    """
    db = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE teatree_loop_state ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "status TEXT NOT NULL, created_at TEXT, updated_at TEXT)"
        )
        if dispatch_status is not None:
            conn.execute("INSERT INTO teatree_loop_state (name, status) VALUES ('dispatch', ?)", (dispatch_status,))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setenv("T3_CONFIG_DB", str(db))


def _write_override(tmp_path: Path, mode: str, *, until: str | None = None) -> None:
    """Write ``availability_override.json`` under the PRIMARY data dir (``tmp_path``)."""
    doc: dict[str, str] = {"mode": mode}
    if until is not None:
        doc["until"] = until
    (tmp_path / "availability_override.json").write_text(json.dumps(doc), encoding="utf-8")


class TestDbPauseLeverReadsViaColdReader:
    """``db_loop_state_suppresses_self_pump`` reads the durable pause via ``cold_reader``."""

    def test_lever_never_imports_in_process_django_bootstrap(self) -> None:
        # The structural #2559 invariant, re-pinned: the lever must NOT import the
        # in-process django bootstrap — it is Django-free (cold_reader + a src
        # bootstrap) so the bare-python3 Stop hook never needs its own
        # ``django.setup()``. A re-introduced bootstrap would silently reinstate
        # the fail-open bug. It also no longer shells out per-lever (fast-hooks).
        assert not hasattr(gate, "bootstrap_teatree_django")
        assert not hasattr(gate, "subprocess")
        assert hasattr(gate, "teatree_src_on_path")

    def test_db_paused_dispatch_suppresses_under_bare_python3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The exact #2559 reproduction: in-process django.setup() is impossible,
        # yet a durable PAUSE on ``dispatch`` is readable via cold_reader.
        _config_db(tmp_path, monkeypatch, dispatch_status="paused")
        assert gate.db_loop_state_suppresses_self_pump() is True

    def test_db_disabled_dispatch_suppresses_under_bare_python3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status="disabled")
        assert gate.db_loop_state_suppresses_self_pump() is True

    def test_db_enabled_dispatch_does_not_suppress(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status="enabled")
        assert gate.db_loop_state_suppresses_self_pump() is False

    def test_absent_row_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status=None)
        assert gate.db_loop_state_suppresses_self_pump() is False

    def test_missing_db_fails_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_CONFIG_DB", str(tmp_path / "nope.sqlite3"))
        assert gate.db_loop_state_suppresses_self_pump() is False


class TestAwayLeverReadsOverrideFileStdlib:
    """``_resolved_away_mode`` reads the manual-override file + no-schedule default in stdlib.

    The router's thin ``_resolved_away_mode`` delegates to the stdlib sibling
    ``availability_away_probe`` (#2559).
    """

    def test_probe_never_imports_in_process_django_bootstrap(self) -> None:
        assert not hasattr(away_probe, "bootstrap_teatree_django")

    def test_away_override_resolves_true_under_bare_python3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_db(tmp_path, monkeypatch)  # establishes the PRIMARY data dir
        _write_override(tmp_path, "away")
        assert router._resolved_away_mode() is True

    def test_present_override_resolves_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch)
        _write_override(tmp_path, "present")
        assert router._resolved_away_mode() is False

    def test_no_override_no_windows_resolves_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default present: no override file, no configured schedule windows.
        _config_db(tmp_path, monkeypatch)
        assert router._resolved_away_mode() is False

    def test_expired_away_override_falls_through_to_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An expired away override is inactive → falls through to the (windowless)
        # schedule → default present. Anti-vacuous: an ACTIVE away override at the
        # same path resolves True (the sibling test above).
        _config_db(tmp_path, monkeypatch)
        _write_override(tmp_path, "away", until="2000-01-01T00:00:00Z")
        assert router._resolved_away_mode() is False

    def test_configured_schedule_delegates_to_t3_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A configured cron window needs croniter (absent from the bare hook), so
        # the away probe delegates to the ``t3`` subprocess for an exact read.
        _config_db(tmp_path, monkeypatch)  # no override → falls to the schedule tier
        (tmp_path / "teatree.toml").write_text(
            '[teatree.availability]\nwindows = ["* 9-16 * * 1-5"]\n', encoding="utf-8"
        )
        calls: list[list[str]] = []

        def _run(argv: list[str], *_a: object, **_k: object) -> SimpleNamespace:
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="availability: mode=away source=schedule", stderr="")

        monkeypatch.setattr(away_probe, "shutil", SimpleNamespace(which=lambda _n: "/usr/local/bin/t3"))
        monkeypatch.setattr(
            away_probe, "subprocess", SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired)
        )

        assert router._resolved_away_mode() is True
        assert any("availability" in " ".join(c) for c in calls)  # delegated to t3 for the schedule


class TestAutonomousAwayLeverStdlib:
    """``autonomous_away`` defers questions but does NOT pause the self-pump (#2544).

    The stdlib probe splits the single away-only read into
    ``resolved_defers_questions`` (away + autonomous_away) and
    ``resolved_pauses_self_pump`` (away only), read here under the same
    bare-``python3`` reproduction as the away-only lever above.
    """

    def test_autonomous_away_override_defers_but_does_not_pause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_db(tmp_path, monkeypatch)
        _write_override(tmp_path, "autonomous_away")
        assert away_probe.resolved_defers_questions() is True
        assert away_probe.resolved_pauses_self_pump() is False

    def test_away_override_defers_and_pauses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch)
        _write_override(tmp_path, "away")
        assert away_probe.resolved_defers_questions() is True
        assert away_probe.resolved_pauses_self_pump() is True

    def test_present_override_neither_defers_nor_pauses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _config_db(tmp_path, monkeypatch)
        _write_override(tmp_path, "present")
        assert away_probe.resolved_defers_questions() is False
        assert away_probe.resolved_pauses_self_pump() is False

    def test_no_override_no_windows_neither_defers_nor_pauses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_db(tmp_path, monkeypatch)
        assert away_probe.resolved_defers_questions() is False
        assert away_probe.resolved_pauses_self_pump() is False

    def test_router_question_deferral_reads_the_split_predicate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # handle_route_away_mode_question must defer under autonomous_away too —
        # it reads _resolved_defers_questions, not the away-only lever.
        _config_db(tmp_path, monkeypatch)
        _write_override(tmp_path, "autonomous_away")
        assert router._resolved_defers_questions() is True

    def test_stop_self_pump_keeps_running_under_autonomous_away(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point of #2544: unlike holiday-away, autonomous-away must NOT
        # suppress the Stop self-pump.
        _config_db(tmp_path, monkeypatch, dispatch_status="enabled")
        _write_override(tmp_path, "autonomous_away")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is True  # autonomous-away: the pump keeps firing


class TestStopSelfPumpEndToEndUnderBarePython3:
    """The whole handler suppresses through a durable pause in the bare context."""

    def test_db_pause_suppresses_pump_even_with_pending_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status="paused")
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True  # paused: no block, the session may end

    def test_away_override_suppresses_pump_even_with_pending_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _config_db(tmp_path, monkeypatch, dispatch_status="enabled")  # loop runnable
        _write_override(tmp_path, "away")  # but the user is away
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True  # away: the pause wins over the standing directive
