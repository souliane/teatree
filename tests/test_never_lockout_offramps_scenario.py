"""Never-lockout golden scenario — every off-ramp passes with the gate ON (#1733).

The never-lockout guarantee for the orchestrator-boundary Agent gate was only a
Python corpus before this change; #1733 adds a declarative golden scenario
(``tests/fixtures/never_lockout_offramps.yaml``) listing every off-ramp + every
always-allowed orchestration tool. This test loads each row and asserts the REAL
hook (``handle_enforce_orchestrator_boundary``) does NOT deny it — even though the
gate is at its new DEFAULT-ON setting (no kill-switch written).

RED-first: revert any single off-ramp (e.g. drop the ``run_in_background``
exemption) and the matching row flips to a deny, turning this golden red.

The golden is DETERMINISTIC (a hook-verdict assertion, not an LLM-judged
behavioral scenario), so it lives under ``tests/fixtures/`` rather than the
metered ``tests/agent_behavior/scenarios/`` catalog.
"""

from pathlib import Path

import pytest
import yaml

from hooks.scripts.hook_router import handle_enforce_orchestrator_boundary

_GOLDEN = Path(__file__).resolve().parent / "fixtures" / "never_lockout_offramps.yaml"


def _rows() -> list[dict]:
    rows = yaml.safe_load(_GOLDEN.read_text(encoding="utf-8"))
    assert isinstance(rows, list), "golden scenario must be a YAML list"
    assert rows, "golden scenario must be non-empty"
    return rows


@pytest.fixture(autouse=True)
def _gate_default_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty home → the gate is at its DEFAULT setting (now ON) with NO kill-switch."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("HOME", str(home))


def _payload(row: dict) -> dict:
    data: dict = {"tool_name": row["tool_name"], "tool_input": row.get("tool_input", {})}
    if row.get("agent_id"):
        data["agent_id"] = row["agent_id"]
    return data


def test_golden_has_required_offramp_coverage() -> None:
    """The golden must cover every off-ramp + always-allowed tool #1733 names."""
    names = {row["name"] for row in _rows()}
    required = {
        "agent_run_in_background_true",
        "agent_fg_ok_token",
        "agent_subagent_context_foreground",
        "send_message_orchestration",
        "ask_user_question_orchestration",
        "skill_load_orchestration",
        "task_dispatch_orchestration",
        "quick_status_bash",
    }
    missing = required - names
    assert not missing, f"never-lockout golden is missing off-ramp rows: {sorted(missing)}"


@pytest.mark.parametrize("row", _rows(), ids=[row["name"] for row in _rows()])
def test_offramp_is_allowed_with_gate_default_on(row: dict, capsys: pytest.CaptureFixture[str]) -> None:
    assert row["expect"] == "allow", "this golden only encodes never-lockout allow rows"
    verdict = handle_enforce_orchestrator_boundary(_payload(row))
    out = capsys.readouterr().out.strip()
    assert verdict is not True, (
        f"NEVER-LOCKOUT regression — off-ramp {row['name']!r} was DENIED with the gate default-ON.\n"
        f"  payload: {_payload(row)!r}\n  deny: {out}"
    )
    assert out == "", f"off-ramp {row['name']!r} must not emit a deny payload"
