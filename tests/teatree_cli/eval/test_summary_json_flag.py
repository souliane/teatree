"""``--summary-json`` drops the publish-safe per-scenario JSON, before any gate exits.

Drives ``run_single_trial`` (the ``t3 eval run`` single-pass body) against a stub
runner — no live model — and asserts the ``--summary-json`` artifact lands with
each scenario's ``triage_class`` and no transcript, and that (like the other
artifacts) it is written BEFORE the red gate exits non-zero.
"""

import json
from pathlib import Path

import pytest

from teatree.cli.eval.single_trial import SingleTrialGates, run_single_trial
from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher

SENTINEL = "SECRET_TRANSCRIPT_LEAK_summary_json"

_NO_GATES = SingleTrialGates(persist=False, baseline=False, gate_regressions=False, gate_cost_regression=False)


def _spec(name: str, *, lane: str = "clean_room") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"),
        ),
        source_path=Path("/tmp/spec.yaml"),
        model="claude-sonnet-4-6",
        lane=lane,
    )


def _failing_run(spec_name: str) -> EvalRun:
    # No matching tool call ⇒ the positive matcher fails ⇒ a behavioral red.
    return EvalRun(
        spec_name=spec_name,
        tool_calls=(EvalToolCall(name="Bash", input={"command": "ls"}, turn=1),),
        text_blocks=(f"reasoning … {SENTINEL}",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.0,
    )


class _StubRunner:
    def run(self, spec: EvalSpec) -> EvalRun:
        return _failing_run(spec.name)


def _call(specs: list[EvalSpec], *, summary_json: Path | None) -> None:
    run_single_trial(
        specs,
        backend="transcript",
        max_turns=None,
        transcript_dir=None,
        require_executed=False,
        max_budget_usd=1.0,
        effort=None,
        parallel=1,
        output_format="text",
        grader=None,
        judge=False,
        transcript_html=None,
        summary_md=None,
        summary_json=summary_json,
        gates=_NO_GATES,
    )


def test_summary_json_written_before_the_red_gate_exits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", lambda *_a, **_k: _StubRunner())
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    out = tmp_path / "eval-heal.json"
    with pytest.raises(SystemExit) as exc:
        _call([_spec("alpha")], summary_json=out)
    assert exc.value.code == 1  # a behavioral red exits non-zero
    # Anti-vacuous: the artifact is on disk even though the gate exited non-zero.
    body = out.read_text(encoding="utf-8")
    payload = json.loads(body)
    assert payload["head_sha"] == "deadbeef"
    assert payload["totals"] == {"total": 1, "passed": 0, "failed": 1, "skipped": 0}
    assert payload["scenarios"][0]["triage_class"] == "behavioral"
    assert SENTINEL not in body
    assert "text_blocks" not in body


def test_no_summary_json_when_path_is_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", lambda *_a, **_k: _StubRunner())
    sentinel = tmp_path / "eval-heal.json"
    with pytest.raises(SystemExit):
        _call([_spec("alpha")], summary_json=None)
    assert not sentinel.exists()
