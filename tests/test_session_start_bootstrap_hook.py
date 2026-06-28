"""Tests for the SessionStart hook handler (tick-dispatch bootstrap).

#718 established a SessionStart hook emitting ``additionalContext``;
#786 WS3 RETIRED the immortal-singleton roster it used to spawn. The
hook now records which single *session* is the loop-tick owner
(Django-free, so the #758/#810 Stop self-pump can gate on it) and emits
a tick-dispatch directive: the loop is the ``t3 loop tick`` cron + WS1
atomic ``claim-next`` + WS2 ``LoopLease``, never a fixed set of
long-lived sub-agents. The ``/rename`` reminder + OSC title stay
owner-only / interactive-TTY-gated.
"""

import json
import os
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase
from django.utils import timezone

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import (
    _OWNER_LOOP,
    _loop_registry_path,
    _prune_dead_owner,
    _read_loop_registry,
    _write_loop_registry,
    handle_session_end_loop_registry,
    handle_session_start_bootstrap,
)


@pytest.fixture(autouse=True)
def _isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the loop registry + tty sink at temp paths so tests never touch real state.

    Autouse so every test is isolated; returns nothing (the few tests that
    need the concrete paths request the ``registry_paths`` fixture).

    The teatree opt-in marker AND the #256 session-start auto-load opt-in are
    forced active: these tests exercise the bootstrap ownership MECHANISM, not
    the per-session opt-in gates (covered by ``test_teatree_opt_in.py``), so
    they run as the opted-in loop owner.
    """
    reg_dir = tmp_path / "data"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
    # Default: no controlling tty (OSC must NOT fire). Tests opt in explicitly.
    monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))
    monkeypatch.setattr(router, "_teatree_active", lambda session_id: True)
    monkeypatch.setattr(router, "_autoload_enabled", lambda: True)


@pytest.fixture
def registry_paths(tmp_path: Path) -> tuple[Path, Path]:
    """The (registry dir, tty sink) pair the ``_isolation`` fixture configured."""
    return tmp_path / "data", tmp_path / "fake-tty"


def _live_pid() -> int:
    """A pid that is alive for the duration of the test (the test process)."""
    return os.getpid()


def _owner_pid() -> int:
    """The pid the handler records as owner — the hook's parent (the session)."""
    return os.getppid()


class TestLoopRegistry:
    def test_registry_path_under_configured_dir(self, registry_paths) -> None:
        reg_dir, _ = registry_paths
        assert _loop_registry_path() == reg_dir / "loop-registry.json"

    def test_read_missing_registry_returns_empty(self) -> None:
        assert _read_loop_registry() == {}

    def test_write_then_read_roundtrip(self) -> None:
        entry = {"session_id": "s1", "agent_id": "a1", "pid": _live_pid()}
        _write_loop_registry({_OWNER_LOOP: entry})
        assert _read_loop_registry() == {_OWNER_LOOP: entry}

    def test_read_corrupt_registry_returns_empty(self) -> None:
        _loop_registry_path().write_text("{ not json", encoding="utf-8")
        assert _read_loop_registry() == {}

    def test_prune_removes_dead_owner(self) -> None:
        # PID 999999 is (almost certainly) not alive.
        reg = {_OWNER_LOOP: {"session_id": "s1", "agent_id": "a1", "pid": 999999}}
        _write_loop_registry(reg)
        assert _prune_dead_owner(_read_loop_registry()) == {}

    def test_prune_keeps_live_owner(self) -> None:
        reg = {_OWNER_LOOP: {"session_id": "s1", "agent_id": "a1", "pid": _live_pid()}}
        _write_loop_registry(reg)
        assert _prune_dead_owner(_read_loop_registry()) == reg

    def test_owner_loop_is_a_single_key_not_a_roster(self) -> None:
        # #786 WS3: the 4-name immortal roster is retired — _OWNER_LOOP is
        # a single tick-owner-session registry key.
        assert isinstance(_OWNER_LOOP, str)
        assert _OWNER_LOOP == "t3-loop-tick-owner"


class TestHandleSessionStartBootstrap:
    def test_no_session_id_produces_no_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({})
        assert capsys.readouterr().out == ""

    def test_fresh_machine_is_tick_owner_no_roster_spawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Tick-dispatch, NOT the retired roster. Per-unit "spawn one fresh
        # bounded sub-agent" IS the model; what must be gone is the
        # immortal-roster vocabulary + names.
        for name in ("t3-main-loop", "t3-review-loop", "t3-cross-review-loop", "t3-bug-hunt"):
            assert name not in ctx
        for retired in ("re-attach", "reattach", "takeover", "resume by", "from its brief"):
            assert retired not in ctx.lower()
        assert "t3 loop tick" in ctx
        assert "t3 loop claim-next" in ctx
        # Owner gets the rename reminder.
        assert "/rename TEATREE LOOP" in ctx

        owner = _read_loop_registry()[_OWNER_LOOP]
        assert owner["session_id"] == "owner-1"
        # Recorded pid is the SESSION process (hook's parent), not the
        # ephemeral hook subprocess (regression: TestOwnerPidIsSession...).
        assert owner["pid"] == _owner_pid()

    def test_owner_records_agent_id_when_present(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-xyz"})
        capsys.readouterr()
        assert _read_loop_registry()[_OWNER_LOOP]["agent_id"] == "agent-xyz"

    def test_second_live_session_stays_idle_no_spawn(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "agent-owner", "pid": _live_pid()}})

        handle_session_start_bootstrap({"session_id": "second-2"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Non-owner: stay idle, never arm a competing tick, never spawn.
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()
        assert "another" in ctx.lower()
        assert "owner" in ctx.lower()
        assert "owner-1" in ctx  # names the live owner session
        assert "do not arm" in ctx.lower() or "stay idle" in ctx.lower()
        # #1073 doc-alignment: the directive must state the gate is now
        # HARD (a non-owner tick SKIPs, not "runs and finds nothing").
        assert "skip" in ctx.lower()
        assert "find nothing to claim" not in ctx.lower()
        assert "take" in ctx.lower()
        assert "over" in ctx.lower()
        # Non-owner must NOT get the rename reminder.
        assert "/rename TEATREE LOOP" not in ctx
        # Ownership is unchanged.
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "owner-1"

    def test_same_session_restart_is_idempotent_still_owner(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "agent-owner", "pid": _live_pid()}})

        handle_session_start_bootstrap({"session_id": "owner-1"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Post-compaction same-session restart: still owner, tick-driven,
        # nothing to re-spawn.
        assert "t3 loop tick" in ctx
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()
        assert "/rename TEATREE LOOP" in ctx
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "owner-1"

    def test_dead_owner_is_reclaimed_by_new_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        _write_loop_registry({_OWNER_LOOP: {"session_id": "dead-owner", "agent_id": "ghost", "pid": 999999}})

        handle_session_start_bootstrap({"session_id": "new-owner"})

        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        # Dead owner pruned -> this session becomes tick-owner (no
        # re-spawn; the cron keeps ticking).
        assert "t3 loop tick" in ctx
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "new-owner"

    def test_owner_with_tty_emits_osc_title(self, registry_paths) -> None:
        _, tty_path = registry_paths
        Path(tty_path).write_text("", encoding="utf-8")

        handle_session_start_bootstrap({"session_id": "owner-1"})

        assert "\033]0;TEATREE LOOP\007" in Path(tty_path).read_text(encoding="utf-8")

    def test_owner_without_tty_does_not_crash_and_skips_osc(
        self, registry_paths, capsys: pytest.CaptureFixture[str]
    ) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1"})
        assert "additionalContext" in json.loads(capsys.readouterr().out)["hookSpecificOutput"]

    def test_non_owner_with_tty_does_not_emit_osc(self, registry_paths) -> None:
        _, tty_path = registry_paths
        Path(tty_path).write_text("", encoding="utf-8")
        _write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "agent-owner", "pid": _live_pid()}})

        handle_session_start_bootstrap({"session_id": "non-owner"})

        assert Path(tty_path).read_text(encoding="utf-8") == ""


class TestOwnerPidIsSessionNotHookSubprocess:
    """Regression: the hook router is an ephemeral subprocess.

    Recording ``os.getpid()`` would store a pid dead before any second
    session starts, so ``_prune_dead_owner`` would always evict the owner
    and every session would re-claim — defeating the single-owner
    invariant. The recorded pid must be the long-lived session process
    (the hook's parent).
    """

    def test_recorded_pid_is_parent_not_self(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1"})
        capsys.readouterr()
        assert _read_loop_registry()[_OWNER_LOOP]["pid"] == os.getppid()

    def test_owner_survives_a_simulated_second_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "agent-1"})
        capsys.readouterr()

        # Session 2 starts while session 1 is still alive -> stay idle,
        # ownership unchanged.
        handle_session_start_bootstrap({"session_id": "owner-2"})
        ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()
        assert "owner-1" in ctx
        assert _read_loop_registry()[_OWNER_LOOP]["session_id"] == "owner-1"


class TestSessionEndReleasesOwnership:
    def test_owner_session_end_clears_slot(self) -> None:
        _write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "a", "pid": _live_pid()}})
        handle_session_end_loop_registry({"session_id": "owner-1"})
        assert _read_loop_registry() == {}

    def test_non_owner_session_end_keeps_slot(self) -> None:
        reg = {_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "a", "pid": _live_pid()}}
        _write_loop_registry(reg)
        handle_session_end_loop_registry({"session_id": "some-other-session"})
        assert _read_loop_registry() == reg

    def test_session_end_no_session_id_is_noop(self) -> None:
        reg = {_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "a", "pid": _live_pid()}}
        _write_loop_registry(reg)
        handle_session_end_loop_registry({})
        assert _read_loop_registry() == reg

    def test_session_end_empty_registry_is_noop(self) -> None:
        handle_session_end_loop_registry({"session_id": "owner-1"})
        assert _read_loop_registry() == {}


class TestSessionStartWiredIntoRouter:
    def test_session_start_in_handlers_table(self) -> None:
        assert "SessionStart" in router._HANDLERS
        assert handle_session_start_bootstrap in router._HANDLERS["SessionStart"]

    def test_session_end_loop_registry_in_handlers_table(self) -> None:
        assert handle_session_end_loop_registry in router._HANDLERS["SessionEnd"]


class TestWs3TickDispatchContract:
    """#786 WS3: SessionStart retires the immortal-singleton roster.

    The loop is now driven by the ``t3 loop tick`` cron + WS1
    ``claim-next`` (DB-claimed work) + WS2 ``LoopLease`` (one tick-owner),
    NOT by SessionStart spawning/re-spawning a fixed roster of long-lived
    sub-agents. The bootstrap directive must therefore NOT instruct
    spawning the now-retired four-name loop roster, NOT use the
    spawn/takeover/resume/re-attach roster vocabulary, point the session
    at tick-dispatch (the cron drives per-unit fresh bounded sub-agents;
    statelessness across ticks is the compaction-proofing), and still
    emit something (a session needs to know the loop is tick-driven and
    whether it is the tick-owner).
    """

    def _ctx(self, capsys: pytest.CaptureFixture[str], session_id: str = "s-1") -> str:
        handle_session_start_bootstrap({"session_id": session_id})
        return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]

    def test_bootstrap_does_not_instruct_spawning_the_roster(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx = self._ctx(capsys)
        # The retired immortal-singleton roster must not be spawned.
        for name in ("t3-main-loop", "t3-review-loop", "t3-cross-review-loop", "t3-bug-hunt"):
            assert name not in ctx, f"retired roster name {name!r} still in bootstrap directive"
        for retired_token in (
            "t3-main-loop",
            "t3-review-loop",
            "t3-cross-review-loop",
            "t3-bug-hunt",
            "re-attach",
            "reattach",
            "takeover",
            "resume by",
            "from its brief",
            "spawn each",
        ):
            assert retired_token not in ctx.lower()

    def test_bootstrap_points_at_tick_dispatch(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx = self._ctx(capsys).lower()
        # The directive must orient the session toward the tick-driven model.
        assert "tick" in ctx
        assert "t3 loop tick" in ctx or "loop tick" in ctx

    def test_bootstrap_still_emits_a_directive(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A session must still be told the loop is tick-driven; empty
        # output would regress observability.
        assert self._ctx(capsys).strip() != ""

    def test_no_session_id_still_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        handle_session_start_bootstrap({})
        assert capsys.readouterr().out == ""


# ── Issue #980: auto-compact kill-switch advisory ─────────────────────


class TestAutocompactAdvisoryIntegration:
    """Pin that the SessionStart handler surfaces the #980 advisory.

    The advisory is the teatree-side workaround for the harness's
    silent auto-compact kill-switch on 1M-capable models (see
    ``teatree.core.autocompact_advisory``). When the env-var combo
    matches the trip condition, the SessionStart handler must append
    the advisory text to ``additionalContext`` so the agent can read
    it the moment the session starts.
    """

    def test_advisory_appended_when_kill_switch_trips(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "25")
        monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
        monkeypatch.delenv("DISABLE_COMPACT", raising=False)
        monkeypatch.delenv("DISABLE_AUTO_COMPACT", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_MODEL", "claude-opus-4-7[1m]")

        handle_session_start_bootstrap({"session_id": "s1", "agent_id": "a1"})

        payload = json.loads(capsys.readouterr().out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "AUTO-COMPACT SILENT KILL-SWITCH" in context
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" in context
        assert "1000000" in context
        assert "#980" in context

    def test_no_advisory_when_window_already_set(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # User already has the fix env var in place — must NOT nag.
        monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "25")
        monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000")
        monkeypatch.setenv("CLAUDE_CODE_MODEL", "claude-opus-4-7[1m]")

        handle_session_start_bootstrap({"session_id": "s2", "agent_id": "a2"})

        payload = json.loads(capsys.readouterr().out)
        assert "AUTO-COMPACT SILENT KILL-SWITCH" not in payload["hookSpecificOutput"]["additionalContext"]

    def test_no_advisory_when_pct_override_unset(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        # No user-expressed threshold → kill-switch doesn't matter to user.
        monkeypatch.delenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_MODEL", "claude-opus-4-7[1m]")

        handle_session_start_bootstrap({"session_id": "s3", "agent_id": "a3"})

        payload = json.loads(capsys.readouterr().out)
        assert "AUTO-COMPACT SILENT KILL-SWITCH" not in payload["hookSpecificOutput"]["additionalContext"]


# ── Issue #1380: evict stale LoopLease owner on session rotation (#1107 follow-up) ──


class TestStaleLeaseEvictionOnSessionRotation(TestCase):
    """SessionStart evicts a stale ``LoopLease`` owner on rotation.

    Compaction rotates the session id. The hook updates the file
    registry to the new id, but the live ``LoopLease`` row name=
    ``loop-owner`` still carries the OLD id with an unexpired
    ``lease_expires_at``. ``CLAUDE_SESSION_ID`` is empty in Bash-tool
    subprocesses (#1107), so the next ``t3 loop tick`` resolves the new
    id via the file registry and the CAS in ``claim_ownership`` fails:
    DB session != new session, lease not expired. The session can never
    own its own loop until ``t3 loop claim --take-over`` runs manually.

    Fix: when ``handle_session_start_bootstrap`` records a new session as
    the tick-owner, orphan any stale DB row (``session_id != new_id``)
    so the next tick CAS-claims it cleanly.
    """

    @pytest.fixture(autouse=True)
    def _hook_isolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reg_dir = tmp_path / "data"
        reg_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
        monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))

    def test_new_session_evicts_stale_db_lease(self) -> None:
        """Post-compaction self-eviction: same pid, rotated session id.

        The session id rotated (compaction), but the OS process is the
        same. ``owner_pid`` matches the current process's parent so the
        eviction path recognises this as a safe same-process self-reclaim
        and orphans the old lease.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # Claim with the pid that the hook will see as ``current_pid``
        # (``os.getppid()`` in the hook body, same value in this call).
        LoopLease.objects.claim_ownership("loop-owner", session_id="old-session", owner_pid=os.getppid())
        assert LoopLease.objects.get(name="loop-owner").session_id == "old-session"

        handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "a"})

        # The stale same-pid lease is orphaned so the next tick from the
        # new session CAS-claims it cleanly.
        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == ""
        assert row.acquired_at is None
        assert row.lease_expires_at is None

    def test_same_session_restart_does_not_evict_own_lease(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="owner-1")
        expiry_before = LoopLease.objects.get(name="loop-owner").lease_expires_at

        handle_session_start_bootstrap({"session_id": "owner-1", "agent_id": "a"})

        # Same-session restart (post-compaction-same-id, or hook re-fire):
        # the session keeps its own claim, no orphaning.
        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "owner-1"
        assert row.lease_expires_at == expiry_before

    def test_non_owner_session_does_not_touch_db_lease(self) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # A different live session already holds the file-registry owner
        # slot. The new session is told to stay idle — it must NOT
        # orphan the DB row (only an *owner* writes the DB).
        _write_loop_registry({_OWNER_LOOP: {"session_id": "live-owner", "agent_id": "a", "pid": os.getpid()}})
        LoopLease.objects.claim_ownership("loop-owner", session_id="live-owner")

        handle_session_start_bootstrap({"session_id": "outsider", "agent_id": "b"})

        # Non-owner branch fired (registry shows the existing owner) —
        # the DB row is untouched.
        assert LoopLease.objects.get(name="loop-owner").session_id == "live-owner"

    def test_eviction_after_rotation_unblocks_claim_ownership(self) -> None:
        """End-to-end repro of the #1107 follow-up bug (post-compaction same pid).

        Pre-fix sequence:

        1. ``old-session`` holds an unexpired DB ``loop-owner`` claim.
        2. Compaction rotates the live session id to ``new-session``
            (same OS process — ``owner_pid`` matches).
        3. ``SessionStart`` writes the new id to the file registry.
        4. ``t3 loop tick`` resolves ``new-session`` via the registry
            fallback (#1107) and calls
            ``claim_ownership("loop-owner", session_id="new-session")``
            — DB still says ``old-session``, lease unexpired, CAS fails.
        5. Tick skips ("loop not owned by this session"); the user must
            run ``t3 loop claim --take-over`` to recover.

        Post-fix: step 3 evicts the same-pid stale row, so step 4 wins.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # (1) old session is the DB lease holder, unexpired, same pid
        # (post-compaction: the OS process did not change).
        LoopLease.objects.claim_ownership(
            "loop-owner", session_id="old-session", ttl_seconds=1800, owner_pid=os.getppid()
        )
        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "old-session"
        assert row.lease_expires_at is not None
        assert row.lease_expires_at > timezone.now() + timedelta(seconds=60)

        # (3) hook records the new session as tick-owner
        handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "a"})

        # (4) new session's first tick CAS-claims cleanly (no take-over)
        won, owner = LoopLease.objects.claim_ownership("loop-owner", session_id="new-session")
        assert won is True
        assert owner == "new-session"


# ── Issue #1604: new-session hijacks live loop-owner lease on registry desync ──


class TestNewSessionHijackFix(TestCase):
    """Registry/DB desync must not let a new session evict a LIVE foreign lease.

    #1604 root cause: ``_evict_stale_db_lease_owner`` runs an unconditional
    ``UPDATE ... WHERE session_id != <new>``, so it orphans even a LIVE
    foreign lease. A LIVE lease must be preserved (INV1). Only an expired
    lease, a same-pid (post-compaction) lease, or a dead-pid lease may be
    evicted.
    """

    @pytest.fixture(autouse=True)
    def _hook_isolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reg_dir = tmp_path / "data"
        reg_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
        monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))

    def test_new_session_with_live_foreign_lease_stays_idle_and_preserves_lease(self) -> None:
        """INV1: a new session must NOT evict a live foreign DB lease.

        Pre-fix (bug): ``_evict_stale_db_lease_owner`` unconditionally
        orphans the DB row regardless of liveness, then the new session
        wins the next CAS and hijacks the live incumbent's loop.

        Post-fix: the ``owner is None`` branch consults
        ``LoopLease.objects.ownership_status("loop-owner")`` before
        deciding; if the DB lease is LIVE and foreign it emits
        ``_TICK_DISPATCH_NON_OWNER_DIRECTIVE`` and does NOT evict.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # Registry is empty (desync: the live incumbent's registry entry
        # was pruned by a failed import or race).
        # DB lease is LIVE and foreign (different session, unexpired).
        LoopLease.objects.claim_ownership("loop-owner", session_id="live-incumbent", ttl_seconds=1800)
        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "live-incumbent"
        assert row.lease_expires_at is not None
        assert row.lease_expires_at > timezone.now() + timedelta(seconds=60)

        handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "b"})

        # INV1: the live foreign lease must be untouched.
        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "live-incumbent", "HIJACK: new-session evicted the live incumbent's DB lease"
        assert row.lease_expires_at is not None

    def test_unknown_registry_but_live_db_lease_does_not_claim(self) -> None:
        """INV4: unknown registry (pruned) + live DB lease → KEEP (stay idle).

        When the file registry returns no entry (empty after prune) but the
        DB shows a LIVE foreign lease, the new session must stay idle — it
        must NOT claim ownership.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # Empty registry (as if pruned by the fail-safe returning {}).
        assert _read_loop_registry() == {}

        # A foreign session holds a live DB lease.
        LoopLease.objects.claim_ownership("loop-owner", session_id="live-foreign", ttl_seconds=1800)

        handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "b"})

        # The new session must not have claimed the DB lease.
        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "live-foreign", "HIJACK: new session claimed ownership despite live foreign DB lease"

    def test_live_foreign_lease_null_pid_stays_idle(self) -> None:
        """INV4: null owner_pid + live foreign lease → KEEP (stay idle)."""
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # Claim with null pid (unknown — old code paths).
        LoopLease.objects.claim_ownership("loop-owner", session_id="foreign", ttl_seconds=1800, owner_pid=None)

        handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "b"})

        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "foreign"

    def test_live_dead_pid_lease_is_evicted(self) -> None:
        """Dead owner pid + live (unexpired) lease → EVICT (owner process gone)."""
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # PID 999999 is almost certainly dead.
        LoopLease.objects.claim_ownership("loop-owner", session_id="dead-owner", ttl_seconds=1800, owner_pid=999999)

        handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "b"})

        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "", "expected dead-pid lease to be evicted"
        assert row.lease_expires_at is None

    def test_expired_foreign_lease_with_unknown_pid_is_evicted(self) -> None:
        """Expired foreign lease + null owner_pid → EVICT (TTL fallback governs).

        With no live ``owner_pid`` to anchor liveness, the TTL is the sole
        release: an expired lease is dead and reclaimable. (An *alive*
        ``owner_pid`` past TTL is the protected busy-owner case covered by
        ``test_alive_owner_pid_expired_ttl_keeps_loop_with_incumbent``.)
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="expired-owner", ttl_seconds=1, owner_pid=None)
        row = LoopLease.objects.get(name="loop-owner")
        row.lease_expires_at = timezone.now() - timedelta(seconds=5)
        row.save(update_fields=["lease_expires_at"])

        handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "b"})

        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "", "expected expired lease to be evicted"

    def test_no_db_lease_new_session_claims(self) -> None:
        """Empty registry + no DB lease → new session claims (normal first start)."""
        from teatree.core.models import LoopLease  # noqa: PLC0415

        assert _read_loop_registry() == {}

        handle_session_start_bootstrap({"session_id": "fresh-session", "agent_id": "a"})

        # Session claimed the DB lease on its first tick.
        won, _ = LoopLease.objects.claim_ownership("loop-owner", session_id="fresh-session")
        assert won is True

    def test_same_session_restart_no_registry_keeps_db_claim(self) -> None:
        """Same session restarted with no registry entry (e.g. boot) keeps its DB lease."""
        from teatree.core.models import LoopLease  # noqa: PLC0415

        assert _read_loop_registry() == {}
        # The session already holds the DB lease from a previous start.
        LoopLease.objects.claim_ownership("loop-owner", session_id="same-session", owner_pid=os.getppid())

        handle_session_start_bootstrap({"session_id": "same-session", "agent_id": "a"})

        # It won the same-session branch — DB claim preserved.
        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "same-session"

    def test_live_foreign_lease_directive_names_owner(self) -> None:
        """The non-owner directive text includes the actual owner session id."""
        import io  # noqa: PLC0415
        import sys  # noqa: PLC0415

        from teatree.core.models import LoopLease  # noqa: PLC0415

        LoopLease.objects.claim_ownership("loop-owner", session_id="live-owner", ttl_seconds=1800)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "b"})
        finally:
            sys.stdout = old_stdout

        payload = json.loads(captured.getvalue())
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "live-owner" in ctx
        assert "another session" in ctx.lower() or "non-owner" in ctx.lower() or "another live session" in ctx.lower()

    def test_django_bootstrap_failure_falls_through_to_claim(self) -> None:
        """DB bootstrap failure → fail-open: new session claims (INV5)."""
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # Simulate bootstrap failure for _db_live_foreign_owner.
        with mock.patch("hooks.scripts.hook_router.bootstrap_teatree_django", return_value=False):
            handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "a"})

        # Session fell through to the owner path (fail-open).
        won, _ = LoopLease.objects.claim_ownership("loop-owner", session_id="new-session")
        assert won is True

    def test_alive_owner_pid_expired_ttl_keeps_loop_with_incumbent(self) -> None:
        """An ALIVE owner_pid past TTL is a live owner — no SessionStart hijack.

        The headline #1073 hijack: the incumbent is alive but busy past the
        tick TTL, so no Stop fires and the lease TTL-lapses while the owner
        process is alive. ``_db_live_foreign_owner`` must recognise the
        alive ``owner_pid`` as a live owner (not TTL-only) and the new
        session must stay idle, leaving the DB row owned by the incumbent.
        """
        from hooks.scripts.hook_router import _db_live_foreign_owner  # noqa: PLC0415
        from teatree.core.models import LoopLease  # noqa: PLC0415

        # Incumbent: alive process pid, but its TTL has lapsed (busy > TTL).
        LoopLease.objects.claim_ownership("loop-owner", session_id="incumbent", ttl_seconds=1, owner_pid=os.getpid())
        row = LoopLease.objects.get(name="loop-owner")
        row.lease_expires_at = timezone.now() - timedelta(seconds=30)
        row.save(update_fields=["lease_expires_at"])

        # The new session is a different OS process.
        live_owner = _db_live_foreign_owner("new-session", current_pid=os.getpid() + 1)
        assert live_owner == "incumbent", "alive owner_pid past TTL must be recognised as a live owner"

        handle_session_start_bootstrap({"session_id": "new-session", "agent_id": "b"})

        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "incumbent", "HIJACK: new-session took over an alive owner's expired-TTL lease"


# ── Issue #1838 PR#7a: evict-on-compact orphans the stale loop-owner lease ──


class TestEvictOnCompactReanchorsLoopOwner(TestCase):
    """The lead's ``SessionStart(source=compact)`` orphans the stale ``loop-owner`` lease.

    The compaction window is the race the maker-only pane layer must close: the
    lead's session id rotates during compaction, and a pane could try to claim
    ``loop-owner`` in that window. The fix calls ``evict_stale_owner`` (keep the
    lead session, current pid) SYNCHRONOUSLY on ``source == "compact"`` — BEFORE
    any tick — which ORPHANS the stale same-pid ``loop-owner`` lease
    (``session_id=""``) so the lead's next ``t3 loop tick`` re-anchors it
    uncontested and no pane can win the compaction-window CAS. (The eviction only
    orphans; the re-claim is the lead's next tick.) ``evict_stale_owner``'s safety
    table still applies: a LIVE foreign lease is preserved.

    must-fire: on ``source == "compact"`` the synchronous eviction runs even on
    the same-session-restart branch (where the rotation-only path does not).
    must-not-fire: a normal start does not trigger the extra synchronous
    eviction, and a live foreign lease is never blanked.
    """

    @pytest.fixture(autouse=True)
    def _hook_isolation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        reg_dir = tmp_path / "data"
        reg_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(reg_dir))
        monkeypatch.setattr(router, "_TTY_PATH", str(tmp_path / "fake-tty"))

    def test_compact_evicts_stale_same_pid_lease_on_same_session_restart(self) -> None:
        """must-fire: a stale same-pid lease is re-anchored on a compact resume.

        The registry already records the lead session as owner (the
        same-session-restart ``else`` branch, where the rotation-only eviction
        does NOT run). A DB ``loop-owner`` row carries an OLD rotated id with the
        SAME pid (post-compaction same process). Without evict-on-compact the
        stale row would linger and the lead's next tick CAS would fail. The
        compact eviction recognises the same-pid lease as a safe self-reclaim
        and orphans it.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        _write_loop_registry({_OWNER_LOOP: {"session_id": "lead", "agent_id": "a", "pid": os.getpid()}})
        LoopLease.objects.claim_ownership(
            "loop-owner", session_id="old-rotated-id", owner_pid=os.getppid(), ttl_seconds=1800
        )

        handle_session_start_bootstrap({"session_id": "lead", "agent_id": "a", "source": "compact"})

        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "", "compact resume must re-anchor the stale same-pid loop-owner lease"
        assert row.owner_pid is None
        assert row.lease_expires_at is None

    def test_normal_start_does_not_run_the_compact_eviction(self) -> None:
        """must-not-fire: a non-compact same-session restart leaves a stale lease untouched.

        Identical setup to the must-fire case but ``source`` is absent (a normal
        start, not a compaction). The synchronous compact eviction must NOT run,
        so the same-session-restart branch leaves the DB row exactly as it was —
        proving the eviction is gated on ``source == "compact"``, not fired on
        every SessionStart.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        _write_loop_registry({_OWNER_LOOP: {"session_id": "lead", "agent_id": "a", "pid": os.getpid()}})
        LoopLease.objects.claim_ownership(
            "loop-owner", session_id="old-rotated-id", owner_pid=os.getppid(), ttl_seconds=1800
        )

        handle_session_start_bootstrap({"session_id": "lead", "agent_id": "a"})

        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "old-rotated-id", "non-compact start must NOT run the synchronous compact eviction"

    def test_compact_preserves_a_live_foreign_lease(self) -> None:
        """A LIVE foreign lease is preserved even on a compact resume (safety table).

        The pane never wins by hijacking a genuinely live foreign owner. When a
        different session holds a live (alive-pid) ``loop-owner`` lease, the
        compact eviction's safety decision table keeps it — the lead stays idle
        rather than stealing the claim.
        """
        from teatree.core.models import LoopLease  # noqa: PLC0415

        _write_loop_registry({_OWNER_LOOP: {"session_id": "other-live-lead", "agent_id": "x", "pid": os.getpid()}})
        LoopLease.objects.claim_ownership(
            "loop-owner", session_id="other-live-lead", owner_pid=os.getpid(), ttl_seconds=1800
        )

        handle_session_start_bootstrap({"session_id": "lead", "agent_id": "a", "source": "compact"})

        row = LoopLease.objects.get(name="loop-owner")
        assert row.session_id == "other-live-lead", "a LIVE foreign lease must be preserved on a compact resume"
