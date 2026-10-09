"""The loop runner is a durable principal — its own next tick is never a stranger (#3810).

The keystone regression. ``t3 worker`` is a long-lived daemon with NO Claude
session, so ``loops_tick``'s ``current_session_id()`` fell through to the loop
registry's ``t3-loop-tick-owner`` record — a shared file every ``SessionStart``
hook rewrites. The runner's identity therefore rotated between its own
consecutive ticks: tick N claimed ``loop:<name>`` under registry id A, the
registry rotated to B, and tick N+1 read B, found A's still-live lease and
SKIPped. Every tick SKIPped until the 1800s TTL lapsed, so a 60s loop ran once
per TTL instead of once per cadence.

Each test drives the REAL command through the REAL spawn-environment seam
(:func:`teatree.loops.deadlined_tick.tick_subprocess_env`) with the session-id
env vars absent — the loop runner container's actual environment — and rotates
the registry between ticks exactly as a fresh ``SessionStart`` would.
"""

import io
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.models import Loop, LoopLease
from teatree.core.session_identity import SESSION_ID_ENV_VARS
from teatree.loop.tick import TickReport
from teatree.loops.deadlined_tick import tick_subprocess_env

_LOOP = "inbox"
_SLOT = f"loop:{_LOOP}"


def _run_tick_under(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> str:
    """Run one ``loops_tick`` with *env* applied, returning its combined output."""
    for key, value in env.items():
        if key.startswith(("T3_LOOP_", "CLAUDE_")):
            monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    call_command("loops_tick", loop=_LOOP, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


@pytest.fixture
def runner_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The loop runner's real environment: no Claude session vars, a live registry file."""
    for name in SESSION_ID_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("T3_LOOP_SESSION_PID", raising=False)
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(registry_dir))
    return registry_dir / "loop-registry.json"


def _dead_pid() -> int:
    """A pid that is provably not running — a departed Claude session's, as the runner sees it.

    The registry records the pid of the Claude session that became tick-owner.
    By the time the loop runner reads that record the session has usually
    exited, and the runner reads the file from inside its own container where
    the recorded HOST pid does not exist in its pid namespace at all. Either
    way the pid is unresolvable, which is the case production actually hits.
    """
    import os  # noqa: PLC0415 — deferred: only the probe needs it

    for candidate in range(4_000_000, 4_000_100):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    pytest.skip("no provably-dead pid available on this host")
    raise AssertionError  # pragma: no cover — pytest.skip raises


def _rotate_registry(path: Path, session_id: str) -> None:
    """Rewrite the tick-owner record exactly as a fresh ``SessionStart`` hook does."""
    path.write_text(
        json.dumps({"t3-loop-tick-owner": {"session_id": session_id, "agent_id": "", "pid": _dead_pid()}}),
        encoding="utf-8",
    )


@pytest.fixture
def _enabled_loop(db: None) -> None:
    Loop.objects.update_or_create(name=_LOOP, defaults={"enabled": True, "delay_seconds": 60})


@pytest.fixture
def _stub_tick() -> Iterator[None]:
    """Stub the tick pipeline — this suite is about the lease, not the scan."""
    import datetime as dt  # noqa: PLC0415 — deferred: only the stub needs it

    report = TickReport(started_at=dt.datetime.now(tz=dt.UTC))
    with (
        patch("teatree.loop.tick.run_tick", return_value=report),
        patch("teatree.loops.connector_preflight.run_loop_connector_preflight"),
    ):
        yield


@pytest.mark.usefixtures("_enabled_loop", "_stub_tick")
class TestConsecutiveRunnerTicks:
    def test_second_tick_runs_after_the_registry_rotates(
        self, runner_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two consecutive ticks of one loop from the SAME runner never SKIP.

        The registry rotation between them is a fresh Claude ``SessionStart`` —
        an event the loop runner neither causes nor participates in. It must not
        cost the runner ownership of the loop it is already driving.
        """
        _rotate_registry(runner_env, "aaaaaaaa-0000-0000-0000-000000000000")
        first = _run_tick_under(tick_subprocess_env(), monkeypatch)
        assert "SKIP" not in first, first

        _rotate_registry(runner_env, "bbbbbbbb-1111-1111-1111-111111111111")
        second = _run_tick_under(tick_subprocess_env(), monkeypatch)

        assert "SKIP" not in second, second

    def test_the_lease_is_anchored_on_a_live_owner_pid(self, runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A runner claim records a checkable ``owner_pid``, never NULL.

        A NULL ``owner_pid`` collapses liveness to the TTL alone: nothing can
        tell a lease held by a live runner from one abandoned by a dead one.
        """
        _rotate_registry(runner_env, "aaaaaaaa-0000-0000-0000-000000000000")
        _run_tick_under(tick_subprocess_env(), monkeypatch)

        lease = LoopLease.objects.get(name=_SLOT)
        assert lease.owner_pid is not None, "the runner's lease must name the process holding it"

    def test_the_runner_identity_is_not_the_registry_session(
        self, runner_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The runner claims under its OWN principal, never a borrowed Claude session id.

        Pins the root cause shut: were the runner to keep borrowing whichever
        session the registry names, the rotation guard above would pass only by
        accident of timing.
        """
        _rotate_registry(runner_env, "aaaaaaaa-0000-0000-0000-000000000000")
        _run_tick_under(tick_subprocess_env(), monkeypatch)

        lease = LoopLease.objects.get(name=_SLOT)
        assert lease.session_id != "aaaaaaaa-0000-0000-0000-000000000000", (
            "the runner borrowed the registry's Claude session id as its own identity"
        )
