"""Tests for session-bound loop durability via the single tick-owner record.

Behavior contract (#786 WS3 — the immortal-singleton roster is RETIRED):

Zero open Claude sessions => the loop is DEAD (ACCEPTED, by design; the
``t3 loop tick`` cron only fires inside a session). The loop is driven by
that cron + WS1 atomic ``claim-next`` + WS2 ``LoopLease``; SessionStart no
longer spawns/re-spawns a fixed roster. SessionStart only records which
single *session* is the loop-tick owner (Django-free, so the #758/#810
Stop self-pump can gate on it). Owner dies / another session opens => the
new session becomes tick-owner and keeps ticking (nothing to re-spawn —
statelessness across ticks is the compaction-proofing). A live concurrent
owner => the second session stays idle (no competing tick), never evicts
the live owner. All registry writes are flock-guarded (serialized), and
the read->decide->write is one flock transaction so two simultaneous
fresh sessions can NEVER both claim ownership.

These exercise the real ``hook_router`` registry + SessionStart/SessionEnd
handlers under a temp ``T3_LOOP_REGISTRY_DIR``.
"""

import json
import multiprocessing
import multiprocessing.synchronize
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _OWNER_LOOP,
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


def _owner_entry(session_id: str, agent_id: str, pid: int) -> dict:
    return {_OWNER_LOOP: {"session_id": session_id, "agent_id": agent_id, "pid": pid}}


class TestTickOwnerRecord:
    """SessionStart records ONE tick-owner session (no roster, #786 WS3)."""

    def test_fresh_claim_records_single_owner(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-a"})
        capsys.readouterr()

        reg = _read_loop_registry()
        assert list(reg) == [_OWNER_LOOP]  # exactly one record, no roster
        entry = reg[_OWNER_LOOP]
        assert entry["session_id"] == "owner-1"
        assert entry["agent_id"] == "agent-a"
        assert entry["pid"] == os.getppid()
        assert "spawn_brief" not in entry  # briefs retired

    def test_no_owner_is_tick_dispatch_not_spawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "first", "agent_id": "a1"})
        ctx = _ctx(capsys).lower()
        assert "t3 loop tick" in ctx
        assert "claim-next" in ctx
        assert "from its brief" not in ctx
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "first"


class TestDeadOwnerReclaim:
    def test_dead_owner_is_reclaimed_no_respawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry(_owner_entry("dead-owner", "ghost", 999999))

        handle_session_start_bootstrap({"session_id": "successor", "agent_id": "agent-s"})

        ctx = _ctx(capsys).lower()
        # Tick-driven: the successor keeps ticking. The directive may say
        # "nothing to re-spawn" (the negation IS the point) — what must be
        # absent is the retired roster vocabulary + the stale ghost
        # agentId being surfaced for resume.
        assert "t3 loop tick" in ctx
        for retired in ("from its brief", "takeover", "resume by", "ghost", "t3-main-loop", "t3-bug-hunt"):
            assert retired not in ctx
        entry = _read_loop_registry()[_OWNER_LOOP]
        assert entry["session_id"] == "successor"
        assert entry["agent_id"] == "agent-s"
        assert entry["pid"] == os.getppid()


class TestConcurrentLiveOwnerStaysIdle:
    def test_second_live_session_stays_idle_no_evict(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry(_owner_entry("owner-1", "agent-owner", _live_pid()))

        handle_session_start_bootstrap({"session_id": "second-2", "agent_id": "agent-2"})

        ctx = _ctx(capsys).lower()
        assert "stay idle" in ctx or "do not arm" in ctx
        assert "owner-1" in ctx  # names the live owner
        owner = _read_loop_registry()[_OWNER_LOOP]
        assert owner["session_id"] == "owner-1"
        assert owner["agent_id"] == "agent-owner"


class TestSameSessionRestartStaysOwner:
    """Post-compaction same-session restart: still owner, keep ticking.

    Nothing to resume-by-agentId (no roster of sub-agents) — the cron
    simply keeps ticking under the same owner session.
    """

    def test_same_session_restart_is_idempotent_owner(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry(_owner_entry("owner-1", "agent-owner", os.getppid()))

        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-owner"})

        ctx = _ctx(capsys).lower()
        assert "t3 loop tick" in ctx
        assert "resume by" not in ctx
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "owner-1"


class TestSessionEndReleasesForImmediateTakeover:
    def test_owner_clean_exit_releases_the_record(self) -> None:
        _write_loop_registry(_owner_entry("owner-1", "a1", os.getppid()))
        handle_session_end_loop_registry({"session_id": "owner-1"})
        assert _OWNER_LOOP not in _read_loop_registry()

    def test_non_owner_exit_keeps_live_owner(self) -> None:
        _write_loop_registry(_owner_entry("owner-1", "a1", os.getppid()))
        handle_session_end_loop_registry({"session_id": "some-other-session"})
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "owner-1"


def _concurrent_writer(reg_dir: str, name: str, count: int) -> None:
    """Child process: hammer flock-guarded registry writes for one key."""
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

    Design-agnostic flock invariant (retained verbatim across the #786
    WS3 roster retirement): the file must always be parseable because the
    flock serializes writers — no torn write, no lost read-modify-write.
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

        raw = (Path(reg_dir) / "loop-registry.json").read_text(encoding="utf-8")
        data = json.loads(raw)  # raises if corrupted
        assert isinstance(data, dict)
        for n in range(4):
            assert f"loop-{n}" in data


def _race_round(
    reg_dir: str,
    session_id: str,
    barrier: "multiprocessing.synchronize.Barrier",
) -> str:
    """Worker call: one brand-new session running the SessionStart bootstrap.

    Both workers block on a shared barrier so they fire as simultaneously
    as the OS scheduler allows. Pre-fix (#718 write-only-lock) the read
    sits OUTSIDE the flock, so both workers read the empty registry, both
    decide "fresh", and BOTH become tick-owner → two competing
    tick-owners. Post-fix the read->decide->write is one flock
    transaction: the second worker blocks until the first commits,
    re-reads, sees the live owner, and emits the NON-owner ("stay idle")
    directive.

    The two workers are persistent across rounds (ProcessPoolExecutor),
    so module import and Django settings load are paid once per worker —
    not 2N times. Per-round re-isolation goes through the env var +
    ``importlib.reload`` so each round sees a fresh registry dir.
    """
    os.environ["T3_LOOP_REGISTRY_DIR"] = reg_dir
    import importlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    from contextlib import redirect_stdout  # noqa: PLC0415

    import hooks.scripts.hook_router as r  # noqa: PLC0415

    importlib.reload(r)
    barrier.wait(timeout=10)
    buf = io.StringIO()
    with redirect_stdout(buf):
        r.handle_session_start_bootstrap({"session_id": session_id, "agent_id": f"a-{session_id}"})
    out = buf.getvalue().strip()
    return json.loads(out)["additionalContext"] if out else ""


def _is_owner_directive(ctx: str) -> bool:
    low = ctx.lower()
    return "loop-tick owner" in low and "stay idle" not in low


def _is_non_owner_directive(ctx: str) -> bool:
    low = ctx.lower()
    return "stay idle" in low or "do not arm" in low


class TestConcurrentFreshClaimIsAtomic:
    """#718 atomic-claim invariant, preserved under #786 WS3.

    Two fresh sessions starting simultaneously against an empty registry
    must NEVER both become tick-owner — a double-claim means two
    competing ticks (duplicate dispatch / PR-creation races). The atomic
    read->decide->write flock transaction guarantees exactly ONE owner
    directive and one non-owner ("stay idle") directive. On the pre-fix
    write-only-lock the read sits outside the flock so both children
    claim — the assertions below then trip, demonstrating this test
    guards the fix. Repeated over many rounds because the bad interleave
    is timing-dependent.
    """

    def test_simultaneous_fresh_starts_never_both_claim(self, tmp_path: Path) -> None:
        rounds = 12
        # Persistent pool of two workers across all rounds: Python startup
        # and ``hook_router`` import are paid ONCE per worker, not 2N times.
        # Without this, ``spawn`` on macOS + Django settings load in each
        # of 24 children pushes the test wall-clock past the 10s
        # ``pytest-timeout`` under Docker/CI load, flaking the pre-push
        # hook. Race correctness is preserved: real OS-level concurrency,
        # shared ``Barrier`` start gate, fresh registry dir per round +
        # ``importlib.reload`` to re-isolate module state.
        ctx = multiprocessing.get_context("spawn")
        manager = ctx.Manager()
        try:
            with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as pool:
                for rnd in range(rounds):
                    reg_dir = str(tmp_path / f"data-{rnd}")
                    Path(reg_dir).mkdir(parents=True, exist_ok=True)
                    barrier = manager.Barrier(2)

                    fut_a = pool.submit(_race_round, reg_dir, "sessionA", barrier)
                    fut_b = pool.submit(_race_round, reg_dir, "sessionB", barrier)
                    ctx_a = fut_a.result(timeout=20)
                    ctx_b = fut_b.result(timeout=20)

                    owners = [c for c in (ctx_a, ctx_b) if _is_owner_directive(c)]
                    idlers = [c for c in (ctx_a, ctx_b) if _is_non_owner_directive(c)]

                    assert len(owners) == 1, (
                        f"round {rnd}: double-claim — {len(owners)} owner directives "
                        f"(A={ctx_a[:50]!r} B={ctx_b[:50]!r})"
                    )
                    assert len(idlers) == 1, f"round {rnd}: loser must stay idle, not claim"

                    data = json.loads((Path(reg_dir) / "loop-registry.json").read_text(encoding="utf-8"))
                    session_ids = {entry["session_id"] for entry in data.values()}
                    assert len(session_ids) == 1, f"round {rnd}: mixed ownership {session_ids}"
                    assert session_ids <= {"sessionA", "sessionB"}
        finally:
            manager.shutdown()
