"""Tests for session-bound loop durability with ownership transfer.

Behavior contract (user-locked, supersedes the rejected launchd design):

Zero open Claude sessions => the loop is DEAD (ACCEPTED, by design; no
OS-scheduler workaround; the CronCreate tick is an in-session
convenience only, never the durability mechanism). Owner session dies
but another session exists/opens => that session becomes the new owner
and TRANSFERS (re-spawns) every registered loop from its persisted
self-contained brief, registering new agentIds. Cross-session takeover
MUST re-spawn-from-brief — agentIds are NOT resumable across different
Claude sessions; resume-by-agentId is ONLY valid same-session (the
compaction path). A live concurrent owner => defer (reattach), never
double-spawn. All registry writes are flock-guarded (serialized).

These exercise the real ``hook_router`` registry + SessionStart/SessionEnd
handlers under a temp ``T3_LOOP_REGISTRY_DIR``.
"""

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    LOOP_AGENT_NAMES,
    _loop_spawn_briefs,
    _read_loop_registry,
    _write_loop_registry,
    handle_session_end_loop_registry,
    handle_session_start_bootstrap,
)


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))


def _live_pid() -> int:
    return os.getpid()


def _ctx(capsys: pytest.CaptureFixture[str]) -> str:
    return json.loads(capsys.readouterr().out)["additionalContext"]


class TestSpawnBriefs:
    """A takeover session can re-spawn a loop it never spawned itself.

    Every registered loop must have a self-contained spawn brief.
    """

    def test_brief_for_every_registered_loop(self) -> None:
        briefs = _loop_spawn_briefs()
        assert set(briefs) == set(LOOP_AGENT_NAMES)

    def test_each_brief_is_self_contained_nonempty(self) -> None:
        for name, brief in _loop_spawn_briefs().items():
            assert isinstance(brief, str)
            assert len(brief) > 40, f"{name} brief too thin to re-spawn from"
            # The brief must name the loop it re-spawns.
            assert name in brief

    def test_main_loop_brief_describes_per_ticket_subagents(self) -> None:
        brief = _loop_spawn_briefs()["t3-main-loop"]
        assert "per ticket" in brief.lower() or "per-ticket" in brief.lower()


class TestRegistryPersistsBriefAndHeartbeat:
    def test_owner_claim_persists_brief_and_heartbeat(self, capsys: pytest.CaptureFixture[str]) -> None:
        before = time.time()
        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-a"})
        capsys.readouterr()

        entry = _read_loop_registry()["t3-main-loop"]
        assert entry["session_id"] == "owner-1"
        assert entry["agent_id"] == "agent-a"
        assert entry["pid"] == os.getppid()
        assert entry["spawn_brief"]  # self-contained, persisted
        assert "t3-main-loop" in entry["spawn_brief"]
        assert entry["heartbeat_ts"] >= int(before)

    def test_all_four_loops_registered_with_briefs(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-a"})
        capsys.readouterr()
        reg = _read_loop_registry()
        for name in LOOP_AGENT_NAMES:
            assert name in reg, f"{name} not registered"
            assert reg[name]["spawn_brief"]


class TestCrossSessionTakeoverReSpawnsFromBrief:
    def test_dead_owner_takeover_emits_respawn_from_brief(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A prior owner registered all loops, then died (dead pid).
        briefs = _loop_spawn_briefs()
        dead = {
            name: {
                "session_id": "dead-owner",
                "agent_id": f"ghost-{name}",
                "pid": 999999,
                "spawn_brief": briefs[name],
                "heartbeat_ts": 1,
            }
            for name in LOOP_AGENT_NAMES
        }
        _write_loop_registry(dead)

        handle_session_start_bootstrap({"session_id": "successor", "agent_id": "agent-s"})

        ctx = _ctx(capsys)
        low = ctx.lower()
        # Cross-session: MUST re-spawn from brief, NOT resume by agentId.
        assert "re-spawn" in low or "respawn" in low or "spawn" in low
        assert "transfer" in low or "take over" in low or "takeover" in low
        # The dead owner's stale agentIds must NOT be presented for resume.
        assert "ghost-t3-main-loop" not in ctx
        # It must explicitly forbid cross-session resume-by-agentId.
        assert "not resumable" in low
        assert "do not attempt to resume by the recorded agent id" in low
        # Every registered loop's brief is carried into the directive.
        for name in LOOP_AGENT_NAMES:
            assert name in ctx

    def test_takeover_registers_new_agent_id_and_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        briefs = _loop_spawn_briefs()
        _write_loop_registry(
            {
                "t3-main-loop": {
                    "session_id": "dead-owner",
                    "agent_id": "ghost",
                    "pid": 999999,
                    "spawn_brief": briefs["t3-main-loop"],
                    "heartbeat_ts": 1,
                }
            }
        )

        handle_session_start_bootstrap({"session_id": "successor", "agent_id": "agent-new"})
        capsys.readouterr()

        entry = _read_loop_registry()["t3-main-loop"]
        assert entry["session_id"] == "successor"
        assert entry["agent_id"] == "agent-new"
        assert entry["pid"] == os.getppid()
        # Brief is preserved across the transfer.
        assert entry["spawn_brief"] == briefs["t3-main-loop"]

    def test_no_owner_at_all_is_fresh_spawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "first", "agent_id": "a1"})
        ctx = _ctx(capsys)
        assert "spawn" in ctx.lower()
        assert _read_loop_registry()["t3-main-loop"]["session_id"] == "first"


class TestConcurrentLiveOwnerDefers:
    def test_second_live_session_defers_no_double_spawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        briefs = _loop_spawn_briefs()
        _write_loop_registry(
            {
                "t3-main-loop": {
                    "session_id": "owner-1",
                    "agent_id": "agent-owner",
                    "pid": _live_pid(),
                    "spawn_brief": briefs["t3-main-loop"],
                    "heartbeat_ts": int(time.time()),
                }
            }
        )

        handle_session_start_bootstrap({"session_id": "second-2", "agent_id": "agent-2"})

        ctx = _ctx(capsys)
        assert "do not spawn" in ctx.lower()
        # Ownership unchanged — the live owner is not evicted.
        assert _read_loop_registry()["t3-main-loop"]["session_id"] == "owner-1"
        assert _read_loop_registry()["t3-main-loop"]["agent_id"] == "agent-owner"


class TestSameSessionCompactionResumesByAgentId:
    """The ONLY place resume-by-agentId is valid is the same session.

    E.g. after context compaction the coordinator re-reads the registry
    and resumes its own loops by their still-valid agentIds.
    """

    def test_same_session_restart_resumes_by_agent_id(self, capsys: pytest.CaptureFixture[str]) -> None:
        briefs = _loop_spawn_briefs()
        _write_loop_registry(
            {
                "t3-main-loop": {
                    "session_id": "owner-1",
                    "agent_id": "agent-owner",
                    "pid": os.getppid(),
                    "spawn_brief": briefs["t3-main-loop"],
                    "heartbeat_ts": 1,
                }
            }
        )

        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-owner"})

        ctx = _ctx(capsys)
        low = ctx.lower()
        # Same session => resume by the still-valid agentId is allowed.
        assert "resume" in low or "spawn" in low
        assert "agent-owner" in ctx
        assert _read_loop_registry()["t3-main-loop"]["session_id"] == "owner-1"


class TestSessionEndReleasesForImmediateTakeover:
    def test_owner_clean_exit_releases_all_slots(self) -> None:
        briefs = _loop_spawn_briefs()
        _write_loop_registry(
            {
                name: {
                    "session_id": "owner-1",
                    "agent_id": f"a-{name}",
                    "pid": os.getppid(),
                    "spawn_brief": briefs[name],
                    "heartbeat_ts": 1,
                }
                for name in LOOP_AGENT_NAMES
            }
        )

        handle_session_end_loop_registry({"session_id": "owner-1"})

        reg = _read_loop_registry()
        # Owner slot released so the next session can immediately take over.
        assert "t3-main-loop" not in reg

    def test_non_owner_exit_keeps_live_owner(self) -> None:
        briefs = _loop_spawn_briefs()
        _write_loop_registry(
            {
                "t3-main-loop": {
                    "session_id": "owner-1",
                    "agent_id": "a1",
                    "pid": os.getppid(),
                    "spawn_brief": briefs["t3-main-loop"],
                    "heartbeat_ts": 1,
                }
            }
        )

        handle_session_end_loop_registry({"session_id": "some-other-session"})

        assert _read_loop_registry()["t3-main-loop"]["session_id"] == "owner-1"


def _concurrent_writer(reg_dir: str, name: str, count: int) -> None:
    """Child process: hammer flock-guarded registry writes for one loop."""
    os.environ["T3_LOOP_REGISTRY_DIR"] = reg_dir
    import importlib  # noqa: PLC0415

    import hooks.scripts.hook_router as r  # noqa: PLC0415

    importlib.reload(r)
    for i in range(count):
        reg = r._read_loop_registry()
        reg[name] = {"session_id": f"{name}-{i}", "agent_id": f"a{i}", "pid": os.getpid()}
        r._write_loop_registry(reg)
        time.sleep(0.001)


class TestRegistryWritesAreFlockSerialized:
    """Concurrent registry writers must not corrupt the JSON.

    Salvaged from the rejected launchd worktree's design-agnostic flock
    invariant: the file must always be parseable because the flock
    serializes writers (no torn write, no lost read-modify-write update).
    """

    def test_concurrent_writers_never_corrupt_registry(self, tmp_path: Path) -> None:
        reg_dir = str(tmp_path / "data")
        Path(reg_dir).mkdir(parents=True, exist_ok=True)

        procs = [multiprocessing.Process(target=_concurrent_writer, args=(reg_dir, f"loop-{n}", 25)) for n in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=20)
            assert p.exitcode == 0

        # The registry file must be valid JSON after concurrent hammering
        # — a torn write (no flock) would leave invalid/truncated JSON.
        raw = (Path(reg_dir) / "loop-registry.json").read_text(encoding="utf-8")
        data = json.loads(raw)  # raises if corrupted
        assert isinstance(data, dict)
        # Every writer's final entry is present (no lost-update from a
        # read-modify-write race under the flock).
        for n in range(4):
            assert f"loop-{n}" in data


def _racing_fresh_start(reg_dir: str, session_id: str) -> None:
    """Child: a brand-new session running the SessionStart bootstrap.

    Two of these race with an EMPTY registry — without the atomic
    read→decide→write transaction both would read "no owner" and both
    would claim, leaving the file owned by whichever wrote last while
    BOTH believe they own (double-claim).
    """
    os.environ["T3_LOOP_REGISTRY_DIR"] = reg_dir
    import importlib  # noqa: PLC0415

    import hooks.scripts.hook_router as r  # noqa: PLC0415

    importlib.reload(r)
    for _ in range(20):
        r.handle_session_start_bootstrap({"session_id": session_id, "agent_id": f"a-{session_id}"})
        time.sleep(0.001)


class TestConcurrentFreshClaimIsAtomic:
    """Finding 1 regression: the read→decide→write must be one flock txn.

    Concurrent fresh-start bootstraps must converge on exactly ONE owner
    session_id (the registry is internally consistent), never a torn or
    mixed-owner registry.
    """

    def test_two_racing_fresh_sessions_yield_a_single_consistent_owner(self, tmp_path: Path) -> None:
        reg_dir = str(tmp_path / "data")
        Path(reg_dir).mkdir(parents=True, exist_ok=True)

        procs = [
            multiprocessing.Process(target=_racing_fresh_start, args=(reg_dir, sid)) for sid in ("sessionA", "sessionB")
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=20)
            assert p.exitcode == 0

        raw = (Path(reg_dir) / "loop-registry.json").read_text(encoding="utf-8")
        data = json.loads(raw)  # never torn — txn holds the flock across the write
        owners = {entry["session_id"] for entry in data.values()}
        # All four loops must be owned by the SAME session (no mixed
        # ownership from an interleaved partial claim).
        assert len(owners) == 1, f"registry has mixed ownership: {owners}"
        assert owners <= {"sessionA", "sessionB"}
