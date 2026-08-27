# test-path: cross-cutting — the directive surfaces span hook_router.py (hooks/) and teatree.cli.loop.
"""Every surface that teaches an agent the loop dispatch cycle also teaches its END.

Four separate prose surfaces tell a slot to claim a unit and spawn a sub-agent for
it. Only the Stop-hook self-pump said what to do when that sub-agent RETURNS, so the
other three kept teaching that a worker's Task is simply "reclaimable and the next
tick re-dispatches it" — true of a worker that DIES, and the exact wrong lesson for
one that returns: nothing terminalizes its unit, so it is reclaimed and re-offered
forever behind a completed counter that never moves.

Presence, not behaviour. The behaviour these stand in for — a late record cannot
finish the generation another tick owns — is
``teatree_core/test_record_attempt_command.py::TestLateRecordCannotFinishAnotherGeneration``.
"""

from pathlib import Path

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _OWNER_LOOP, _TICK_DISPATCH_OWNER_DIRECTIVE, _durable_session_snapshot
from teatree.cli.loop import app as loop_cli

_RECORD_STEP = "record-attempt"


@pytest.fixture(autouse=True)
def _own_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T3_LOOP_REGISTRY_DIR", str(tmp_path))
    router._write_loop_registry({_OWNER_LOOP: {"session_id": "owner-1", "agent_id": "a", "pid": 1}})


def test_the_tick_dispatch_owner_directive_names_the_record_step() -> None:
    assert _RECORD_STEP in _TICK_DISPATCH_OWNER_DIRECTIVE


def test_the_loop_cli_help_names_the_record_step() -> None:
    assert _RECORD_STEP in (loop_cli.loop_app.info.help or "")


def test_the_loop_cli_module_docstring_names_the_record_step() -> None:
    assert _RECORD_STEP in (loop_cli.__doc__ or "")


def test_the_recovery_snapshots_loop_assignment_names_the_record_step() -> None:
    snapshot = _durable_session_snapshot("owner-1")

    assert "## Loop assignment" in snapshot
    assert _RECORD_STEP in snapshot
