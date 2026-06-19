"""The Stop self-pump pause levers must read durable state WITHOUT django.setup() (#2559).

The Stop hook is invoked as a bare ``python3`` (``hooks.json``): the harness
never sources the user's shell profile and the interpreter is whatever the
harness picks — it has NO ``uv`` env, so teatree's dependencies (Django et al.)
are not importable. ``django_bootstrap.bootstrap_teatree_django()`` therefore
returns ``False`` in the real Stop context.

Before the fix, both durable pause levers gated their read on that bootstrap —
``db_loop_state_suppresses_self_pump`` (the DB ``LoopState`` 'pause everything' of
the always-on ``dispatch`` loop, via ``t3 loop pause`` / migration 0087) and
``_resolved_away_mode`` (``t3 teatree availability away``) —
each returned ``False`` when the bootstrap failed — i.e. fail-OPEN. So under the
real bare-``python3`` Stop hook a durable DB pause / away override was SILENTLY
INEFFECTIVE at suppressing the self-pump: the pump kept firing through a pause.

The fix makes both levers stdlib-only — they subprocess the ``t3`` CLI (the
editable install carries its own venv, so it bootstraps Django in a CHILD
process) exactly the way ``_consolidated_pending_work`` already does, instead of
importing teatree in the bare hook interpreter. These tests reproduce the exact
bare-``python3`` context (bootstrap fails) and prove a durable pause now
suppresses the pump: RED before the fix, GREEN after.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _OWNER_LOOP, _write_loop_registry, handle_loop_self_pump

# ``hook_router`` puts its own dir on ``sys.path`` and binds the gate functions
# via bare ``from <sibling> import …`` — that creates SEPARATE module instances
# from ``hooks.scripts.<sibling>`` (the dual identity called out in
# ``hooks/CLAUDE.md``). Patch the instances the router actually calls so the
# stdlib subprocess stubs land on the live code paths.
gate = sys.modules["loop_state_self_pump_gate"]
away_probe = sys.modules["availability_away_probe"]


@pytest.fixture(autouse=True)
def _bare_python3_stop_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the real bare-``python3`` Stop hook context.

    The hook interpreter cannot ``django.setup()`` — so BOTH levers' shared
    bootstrap is forced to fail, exactly as it does in production. Everything
    else (state dir, registry, bash-env fallback) is redirected into ``tmp_path``
    so a developer's real config never leaks into the test.
    """
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router, "STATE_DIR", state)
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TEATREE_BASH_ENV_FILE", str(tmp_path / "no-bash-env"))
    # The defining property of the bare-python3 Stop hook: teatree is NOT
    # importable in the hook interpreter, so the shared django bootstrap returns
    # False. The fixed levers no longer call it (they subprocess ``t3`` so a
    # CHILD process bootstraps Django) — but the router still imports it for
    # OTHER handlers, so force it False there to prove the away lever does NOT
    # depend on an in-process django.setup(). This is the exact production
    # reality the #2559 bug lived in: a False bootstrap.
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


def _fake_t3(
    monkeypatch: pytest.MonkeyPatch,
    target: object,
    responses: dict[str, tuple[int, str]],
) -> list[list[str]]:
    """Stub ``shutil.which('t3')`` + ``subprocess.run`` on *target*'s module.

    *responses* maps a marker substring of the argv (e.g. ``"loop-state"`` or
    ``"availability"``) to a ``(returncode, stdout)`` pair. The captured argv
    list is returned so a test can assert the lever shelled out to ``t3``
    instead of touching the ORM.
    """
    calls: list[list[str]] = []

    monkeypatch.setattr(target, "shutil", SimpleNamespace(which=lambda _name: "/usr/local/bin/t3"))

    def _run(argv: list[str], *_args: object, **_kwargs: object) -> SimpleNamespace:
        calls.append(list(argv))
        joined = " ".join(argv)
        for marker, (code, out) in responses.items():
            if marker in joined:
                return SimpleNamespace(returncode=code, stdout=out, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(target, "subprocess", SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired))
    return calls


class TestDbPauseLeverIsStdlibOnly:
    """``db_loop_state_suppresses_self_pump`` reads the durable pause via ``t3``."""

    def test_gate_module_imports_no_django_bootstrap(self) -> None:
        # The structural #2559 invariant: the gate must NOT import the in-process
        # django bootstrap at all — it is stdlib-only (shutil + subprocess + json)
        # so the bare-python3 Stop hook never needs a ``django.setup()`` of its
        # own. A re-introduced bootstrap import would silently reinstate the
        # fail-open bug, so guard it here.
        assert not hasattr(gate, "bootstrap_teatree_django")
        assert hasattr(gate, "shutil")
        assert hasattr(gate, "subprocess")

    def test_db_paused_dispatch_suppresses_under_bare_python3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The exact #2559 reproduction: django.setup() is impossible (bootstrap
        # forced False by the fixture), yet a durable PAUSE on the ``dispatch``
        # loop is readable via the ``t3`` subprocess. The lever MUST suppress.
        calls = _fake_t3(
            monkeypatch,
            gate,
            {"loop-state": (0, json.dumps({"name": "dispatch", "status": "paused"}))},
        )

        assert gate.db_loop_state_suppresses_self_pump() is True
        assert any("loop-state" in " ".join(c) for c in calls)  # shelled out, did not ORM

    def test_db_disabled_dispatch_suppresses_under_bare_python3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_t3(
            monkeypatch,
            gate,
            {"loop-state": (0, json.dumps({"name": "dispatch", "status": "disabled"}))},
        )
        assert gate.db_loop_state_suppresses_self_pump() is True

    def test_db_enabled_dispatch_does_not_suppress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_t3(
            monkeypatch,
            gate,
            {"loop-state": (0, json.dumps({"name": "dispatch", "status": "enabled"}))},
        )
        assert gate.db_loop_state_suppresses_self_pump() is False

    def test_t3_absent_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No ``t3`` on PATH ⇒ genuinely unreadable ⇒ fail OPEN (do not suppress),
        # so the other gates still decide and the pump never crashes the session.
        monkeypatch.setattr(gate, "shutil", SimpleNamespace(which=lambda _name: None))
        assert gate.db_loop_state_suppresses_self_pump() is False

    def test_t3_error_exit_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_t3(monkeypatch, gate, {"loop-state": (1, "")})
        assert gate.db_loop_state_suppresses_self_pump() is False


class TestAwayLeverIsStdlibOnly:
    """``_resolved_away_mode`` reads the resolved availability via ``t3``.

    The router's thin ``_resolved_away_mode`` delegates to the stdlib sibling
    ``availability_away_probe`` (#2559), so the subprocess stub patches that
    module — the instance the live delegate actually shells out from.
    """

    def test_probe_imports_no_django_bootstrap(self) -> None:
        # Structural #2559 invariant: the away probe is stdlib-only.
        assert not hasattr(away_probe, "bootstrap_teatree_django")
        assert hasattr(away_probe, "shutil")
        assert hasattr(away_probe, "subprocess")

    def test_away_override_resolves_true_under_bare_python3(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_t3(
            monkeypatch,
            away_probe,
            {"availability": (0, "availability: mode=away source=override")},
        )
        assert router._resolved_away_mode() is True

    def test_present_resolves_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_t3(
            monkeypatch,
            away_probe,
            {"availability": (0, "availability: mode=present source=default")},
        )
        assert router._resolved_away_mode() is False

    def test_t3_absent_resolves_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(away_probe, "shutil", SimpleNamespace(which=lambda _name: None))
        assert router._resolved_away_mode() is False


class TestStopSelfPumpEndToEndUnderBarePython3:
    """The whole handler suppresses through a durable pause in the bare context."""

    def test_db_pause_suppresses_pump_even_with_pending_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The end-to-end #2559 proof: bootstrap fails (bare python3), there is
        # pending work, this session owns the loop, availability is present — but
        # a durable DB PAUSE on ``dispatch`` is readable via ``t3``. The Stop
        # self-pump must NOT block (the pause is honoured).
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        def _which(name: str) -> str | None:
            return "/usr/local/bin/t3" if name == "t3" else None

        def _run(argv: list[str], *_args: object, **_kwargs: object) -> SimpleNamespace:
            joined = " ".join(argv)
            if "loop-state" in joined:
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"name": "dispatch", "status": "paused"}), stderr=""
                )
            if "availability" in joined:
                return SimpleNamespace(returncode=0, stdout="availability: mode=present source=default", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        for mod in (gate, away_probe):
            monkeypatch.setattr(mod, "shutil", SimpleNamespace(which=_which))
            monkeypatch.setattr(mod, "subprocess", SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired))

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True  # paused: no block, the session may end

    def test_away_override_suppresses_pump_even_with_pending_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _own_loop("owner-1")
        _fake_pending(monkeypatch, [{"task_id": 4, "subagent": "x", "phase": "coding", "issue_url": "u"}])

        def _which(name: str) -> str | None:
            return "/usr/local/bin/t3" if name == "t3" else None

        def _run(argv: list[str], *_args: object, **_kwargs: object) -> SimpleNamespace:
            joined = " ".join(argv)
            if "loop-state" in joined:
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps({"name": "dispatch", "status": "enabled"}), stderr=""
                )
            if "availability" in joined:
                return SimpleNamespace(returncode=0, stdout="availability: mode=away source=override", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        for mod in (gate, away_probe):
            monkeypatch.setattr(mod, "shutil", SimpleNamespace(which=_which))
            monkeypatch.setattr(mod, "subprocess", SimpleNamespace(run=_run, TimeoutExpired=subprocess.TimeoutExpired))

        result = handle_loop_self_pump({"session_id": "owner-1"})

        assert result is not True  # away: the pause wins over the standing directive
