"""A kill-switch row and an escape token stored under the legacy names still steer the real hook.

The live control DB holds a row for ``orchestrator_bash_gate_enabled`` and about a thousand
``orchestrator_bash`` escape rows, and open sessions keep emitting tokens under the three
gate ids. Both are read by the hook itself, so the proof runs the hook: a subprocess for the
kill-switch (the exit code is the verdict) and the audited-escape path for the tokens, each
paired with the run that does NOT carry the row or token and must still be refused.
"""

import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

import hooks.scripts.hook_router as router
from hooks.scripts import orchestrator_delegation_gate as delegation_gate
from hooks.scripts.session_lane import LANE_INTERACTIVE_CLI
from teatree.core.models.gate_escape import GateEscape
from tests._gate_escape_audit import audited_escape

HOOK_ROUTER = Path(__file__).resolve().parents[2] / "hooks" / "scripts" / "hook_router.py"
HEAVY_BASH = {"tool_name": "Bash", "tool_input": {"command": "uv run pytest"}}
FOREGROUND_AGENT = {"tool_name": "Agent", "tool_input": {"description": "implement X", "run_in_background": False}}


def seed_config_db(path: Path, rows: dict[str, object]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE teatree_config_setting "
            "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', ?, ?)",
            [(key, json.dumps(value)) for key, value in rows.items()],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def run_pretooluse_hook(tmp_path: Path, payload: dict, stored: dict[str, object]) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "hook-home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("T3_", "CLAUDE"))}
    env |= {"HOME": str(home), "T3_CONFIG_DB": str(seed_config_db(tmp_path / "config.sqlite3", stored))}
    request = payload | {"session_id": "sess-legacy-switch", "cwd": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(HOOK_ROUTER), "--event", "PreToolUse"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
    )


@pytest.mark.parametrize(
    ("payload", "stored", "refused_by"),
    [
        pytest.param(HEAVY_BASH, {}, "orchestrator_bash", id="bash-gate-armed-without-a-row"),
        pytest.param(
            HEAVY_BASH,
            {"orchestrator_bash_gate_enabled": True},
            "orchestrator_bash",
            id="bash-gate-armed-by-a-true-row",
        ),
        pytest.param(HEAVY_BASH, {"orchestrator_bash_gate_enabled": False}, None, id="bash-gate-off-by-a-false-row"),
        pytest.param(FOREGROUND_AGENT, {}, "orchestrator_foreground_dispatch", id="agent-gate-armed-without-a-row"),
        pytest.param(
            FOREGROUND_AGENT,
            {"orchestrator_boundary_agent_gate_enabled": False},
            None,
            id="agent-gate-off-by-a-false-row",
        ),
    ],
)
def test_a_row_stored_under_the_legacy_key_steers_the_hook_verdict(
    tmp_path: Path, payload: dict, stored: dict[str, object], refused_by: str | None
) -> None:
    result = run_pretooluse_hook(tmp_path, payload, stored)

    if refused_by is None:
        assert (result.returncode, result.stdout) == (0, "")
    else:
        assert result.returncode == 2
        assert json.loads(result.stdout)["gate_id"] == refused_by


@pytest.fixture
def armed_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "armed-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("T3_CONFIG_DB", str(seed_config_db(tmp_path / "config.sqlite3", {})))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ATTENDED", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_CHILD_SESSION", raising=False)
    with (
        patch.object(delegation_gate, "session_lane", lambda: LANE_INTERACTIVE_CLI),
        patch.object(delegation_gate, "_engaged", lambda _sid: True),
        patch.object(router, "_is_self_rescue", lambda _cmd: False),
        patch.object(router, "_danger_gate_fail_open_enabled", lambda: False),
    ):
        yield tmp_path


def sweep(command: str) -> dict:
    return {"session_id": "sess-legacy-switch", "tool_name": "Bash", "tool_input": {"command": command}}


def test_a_false_row_stored_under_the_legacy_delegation_key_lets_a_sweep_through(
    armed_gates: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert router.handle_block_undelegated_investigation(sweep("rg pattern src/")) is True
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["gate_id"] == "orchestrator_delegation_gate"

    disabled = seed_config_db(armed_gates / "disabled.sqlite3", {"orchestrator_delegation_gate_enabled": False})
    monkeypatch.setenv("T3_CONFIG_DB", str(disabled))

    assert router.handle_block_undelegated_investigation(sweep("rg pattern src/")) is False


@pytest.mark.usefixtures("armed_gates")
class TestALegacyEscapeTokenIsAcceptedAndRecordedUnderTheSameId(TestCase):
    def _assert_the_token_unblocks_exactly_the_tokened_call(
        self, handler: Callable[[dict], bool], tokened: dict, plain: dict, gate: str, reason: str
    ) -> None:
        with audited_escape(tokened, via_cli=True):
            assert handler(tokened) is False
        with audited_escape(plain, via_cli=True), patch("sys.stdout", StringIO()):
            assert handler(plain) is True

        row = GateEscape.objects.get()
        assert (row.gate, row.reason, row.issuer_kind) == (gate, reason, GateEscape.Issuer.AGENT)

    def test_orchestrator_bash(self) -> None:
        command = "uv run pytest"
        self._assert_the_token_unblocks_exactly_the_tokened_call(
            router.handle_enforce_orchestrator_boundary,
            {
                **HEAVY_BASH,
                "tool_input": {"command": f"{command} [gate-escape:orchestrator_bash: need the output inline]"},
            },
            {**HEAVY_BASH, "tool_use_id": "call-plain"},
            "orchestrator_bash",
            "need the output inline",
        )

    def test_orchestrator_foreground_dispatch(self) -> None:
        tokened = {
            "tool_name": "Agent",
            "tool_input": {
                "prompt": "[gate-escape:orchestrator_foreground_dispatch: a bounded probe] implement X",
                "run_in_background": False,
            },
        }
        self._assert_the_token_unblocks_exactly_the_tokened_call(
            router.handle_enforce_orchestrator_boundary,
            tokened,
            {**FOREGROUND_AGENT, "tool_use_id": "call-plain"},
            "orchestrator_foreground_dispatch",
            "a bounded probe",
        )

    def test_orchestrator_delegation_gate(self) -> None:
        self._assert_the_token_unblocks_exactly_the_tokened_call(
            router.handle_block_undelegated_investigation,
            sweep("rg pattern src/  # [gate-escape:orchestrator_delegation_gate: sizing the blast radius]"),
            sweep("rg pattern src/") | {"tool_use_id": "call-plain"},
            "orchestrator_delegation_gate",
            "sizing the blast radius",
        )
