"""Tests for the per-agent TODO-consolidation loop (#786 WS4 — invariant 3).

The #786 acceptance contract, invariant 3:

    Exactly ONE TODO-consolidation loop per agent/sub-agent — per-actor,
    deduped by agent identity across ALL sessions (not per-session, not a
    global singleton). Subsumes board #50 and #789.

The TODO-consolidation loop IS the Stop self-pump. Before WS4 the pump
gated on the single global tick-owner session (``_session_owns_loop``):
that *collapsed it to one global* loop (only the one tick-owner ever
pumped) and keyed anti-spin by ``session_id`` (so one agent spanning two
sessions armed two independent markers — *duplicated when one agent spans
sessions*). Both halves of the acceptance criterion were violated.

WS4 introduces a per-agent consolidation registry (flock-serialized JSON,
reusing the WS3 ``_loop_registry_txn`` substrate, keyed by ``agent_id``)
so the self-pump is exactly one loop per distinct agent identity across
all sessions.

Integration-style: real ``hook_router`` handlers, real ``STATE_DIR`` +
``T3_LOOP_REGISTRY_DIR`` redirected to ``tmp_path``; only the
``pending-spawn`` subprocess (an external boundary) is faked.
"""

import importlib
import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _claim_agent_consolidation_slot,
    _consolidation_registry_path,
    handle_loop_self_pump,
    handle_session_end_self_pump,
)


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)


def _fake_pending(monkeypatch: pytest.MonkeyPatch, entries: list[dict]) -> None:
    monkeypatch.setattr(router, "_consolidated_pending_work", lambda: entries)


def _decision(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else {}


_ONE_UNIT = [{"task_id": 7, "subagent": "t3:orchestrator", "phase": "coding", "issue_url": "u"}]


class TestExactlyOnePerAgentIdentity:
    """Invariant 3: one consolidation loop per distinct agent, across sessions."""

    def test_same_agent_two_sessions_only_one_pumps(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_pending(monkeypatch, _ONE_UNIT)

        first = handle_loop_self_pump({"session_id": "sess-A", "agent_id": "agent-1"})
        assert first is True
        assert _decision(capsys).get("decision") == "block"

        # A different session of the SAME agent must NOT also pump — the
        # consolidation loop is deduped by agent identity across sessions.
        second = handle_loop_self_pump({"session_id": "sess-B", "agent_id": "agent-1"})
        assert second is not True
        assert _decision(capsys) == {}

    def test_distinct_agents_each_get_their_own_loop(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_pending(monkeypatch, _ONE_UNIT)

        a = handle_loop_self_pump({"session_id": "s1", "agent_id": "agent-alpha"})
        assert a is True
        assert _decision(capsys).get("decision") == "block"

        # A DISTINCT agent identity is NOT collapsed into the first
        # agent's loop — it gets its own consolidation loop.
        b = handle_loop_self_pump({"session_id": "s2", "agent_id": "agent-beta"})
        assert b is True
        assert _decision(capsys).get("decision") == "block"

    def test_not_collapsed_to_one_global_owner(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-tick-owner agent still runs its own consolidation loop.

        Pre-WS4 the pump required ``_session_owns_loop`` (the single
        global tick-owner). That collapsed the consolidation loop to one
        global. WS4: any agent with pending work pumps its own loop,
        regardless of who owns the tick.
        """
        _fake_pending(monkeypatch, _ONE_UNIT)

        result = handle_loop_self_pump({"session_id": "not-the-tick-owner", "agent_id": "worker-7"})

        assert result is True
        assert _decision(capsys).get("decision") == "block"


class TestAntiSpinKeyedByAgentNotSession:
    def test_same_agent_spanning_sessions_shares_anti_spin(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One agent across two sessions arms exactly one anti-spin marker.

        Pre-WS4 the marker keyed on ``session_id`` so the same agent in a
        second session re-pumped immediately (duplicate). WS4 keys it on
        ``agent_id``.
        """
        _fake_pending(monkeypatch, _ONE_UNIT)
        handle_loop_self_pump({"session_id": "sess-A", "agent_id": "agent-9"})
        capsys.readouterr()

        # Same agent, fresh session, within the min-interval: deduped.
        result = handle_loop_self_pump({"session_id": "sess-B", "agent_id": "agent-9"})
        assert result is not True
        assert _decision(capsys) == {}

    def test_anti_spin_releases_after_min_interval(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_pending(monkeypatch, _ONE_UNIT)
        handle_loop_self_pump({"session_id": "s1", "agent_id": "agent-x"})
        capsys.readouterr()

        marker = router.STATE_DIR / "agent-x.pump-armed"
        old = time.time() - router._SELF_PUMP_MIN_INTERVAL - 5
        os.utime(marker, (old, old))

        result = handle_loop_self_pump({"session_id": "s1", "agent_id": "agent-x"})
        assert result is True
        assert _decision(capsys).get("decision") == "block"


class TestNoWorkNoSession:
    def test_no_pending_work_does_not_pump(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_pending(monkeypatch, [])
        result = handle_loop_self_pump({"session_id": "s1", "agent_id": "a1"})
        assert result is not True
        assert _decision(capsys) == {}

    def test_no_session_id_is_noop(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_pending(monkeypatch, _ONE_UNIT)
        result = handle_loop_self_pump({"session_id": "", "agent_id": "a1"})
        assert result is not True
        assert _decision(capsys) == {}

    def test_missing_agent_id_falls_back_to_session_scope(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ``agent_id`` in the payload ⇒ the session id is the actor key.

        The Stop payload does not always carry ``agent_id``; absent it,
        the session is its own actor (one loop per session is the
        degenerate-but-correct case of "one per agent identity").
        """
        _fake_pending(monkeypatch, _ONE_UNIT)
        result = handle_loop_self_pump({"session_id": "lonely-session"})
        assert result is True
        assert _decision(capsys).get("decision") == "block"


class TestSessionEndClearsAgentMarker:
    def test_session_end_removes_agent_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_pending(monkeypatch, _ONE_UNIT)
        handle_loop_self_pump({"session_id": "s1", "agent_id": "agent-end"})
        marker = router.STATE_DIR / "agent-end.pump-armed"
        assert marker.is_file()

        handle_session_end_self_pump({"session_id": "s1", "agent_id": "agent-end"})

        assert not marker.exists()

    def test_session_end_no_session_id_is_noop(self) -> None:
        handle_session_end_self_pump({"session_id": "", "agent_id": "x"})  # must not raise


def _race_claim(args: tuple[str, str, str]) -> bool:
    registry_dir, agent_id, session_id = args
    os.environ["T3_LOOP_REGISTRY_DIR"] = registry_dir
    # Each spawned worker reloads the module so the registry path picks
    # up this process's T3_LOOP_REGISTRY_DIR (the flock CAS itself is
    # what the race exercises).
    importlib.reload(router)
    return router._claim_agent_consolidation_slot(agent_id, session_id)


class TestConcurrentClaimIsAtomic:
    """Two concurrent claims for the SAME agent: exactly one wins.

    File-flock CAS (the WS3 ``_loop_registry_txn`` substrate) — not a DB
    race, so the SQLite-prod-backend rule does not apply; this is a real
    multiprocessing flock race over the consolidation registry.
    """

    def test_two_processes_same_agent_one_winner(self, tmp_path: Path) -> None:
        registry_dir = str(tmp_path / "data")
        Path(registry_dir).mkdir(parents=True, exist_ok=True)
        args = [(registry_dir, "race-agent", f"sess-{i}") for i in range(8)]
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(4) as pool:
            results = pool.map(_race_claim, args)
        assert sum(1 for r in results if r) == 1


class TestRegistryPathIsSeparateFromTickOwner:
    def test_consolidation_registry_is_a_distinct_file(self) -> None:
        assert _consolidation_registry_path().name != router._loop_registry_path().name
        assert _consolidation_registry_path().parent == router._loop_registry_path().parent


class TestClaimSemantics:
    def test_claim_true_then_dedup_false_same_agent_other_session(self) -> None:
        assert _claim_agent_consolidation_slot("ag", "sess-1") is True
        assert _claim_agent_consolidation_slot("ag", "sess-2") is False

    def test_claim_idempotent_for_same_agent_same_session(self) -> None:
        assert _claim_agent_consolidation_slot("ag", "sess-1") is True
        assert _claim_agent_consolidation_slot("ag", "sess-1") is True

    def test_dead_holder_pid_is_reclaimable(self, tmp_path: Path) -> None:
        path = _consolidation_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ag": {"agent_id": "ag", "session_id": "old", "pid": 999999, "heartbeat_ts": 0}}),
            encoding="utf-8",
        )
        # The recorded holder pid is dead ⇒ a new session reclaims it.
        assert _claim_agent_consolidation_slot("ag", "new-session") is True

    def test_malformed_registry_file_is_treated_as_empty(self) -> None:
        path = _consolidation_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not valid json", encoding="utf-8")
        # A torn/corrupt file must not crash the Stop path — it reads as
        # an empty registry, so the claim still succeeds.
        assert _claim_agent_consolidation_slot("ag", "sess-1") is True

    def test_release_when_session_holds_nothing_is_a_noop(self) -> None:
        assert _claim_agent_consolidation_slot("ag", "owning-session") is True
        before = _consolidation_registry_path().read_text(encoding="utf-8")
        # A session that holds no consolidation entry releasing is a
        # no-op — the registry is left byte-for-byte unchanged (no
        # gratuitous rewrite).
        router._release_agent_consolidation_slot("a-session-that-holds-nothing")
        assert _consolidation_registry_path().read_text(encoding="utf-8") == before
