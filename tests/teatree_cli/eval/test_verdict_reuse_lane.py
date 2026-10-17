import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.run_dispatch import ResolvedRun, dispatch_resolved_run
from teatree.eval.models import COST_SOURCE_REPORTED, EvalRun, EvalSpec
from teatree.eval.verdict_cache import VerdictLedger


def test_recorded_pass_skips_runner_on_second_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = tmp_path / "SKILL.md"
    agent.write_text("## Graded\nrule\n", encoding="utf-8")
    spec = EvalSpec(
        name="one",
        scenario="case",
        agent_path=str(agent),
        agent_sections=("Graded",),
        prompt="act",
        matchers=(),
        source_path=tmp_path / "one.yaml",
    )
    ledger_path = tmp_path / "verdicts.json"
    summary_path = tmp_path / "summary.json"
    markdown_path = tmp_path / "summary.md"
    calls: list[str] = []

    class Runner:
        def run(self, item: EvalSpec) -> EvalRun:
            calls.append(item.name)
            return EvalRun(
                spec_name=item.name,
                tool_calls=(),
                text_blocks=("done",),
                terminal_reason="success",
                is_error=False,
                raw_stdout="",
                raw_stderr="",
                cost_usd=0.01,
                cost_source=COST_SOURCE_REPORTED,
            )

    monkeypatch.setattr("teatree.cli.eval.single_trial.make_runner", lambda *_args, **_kwargs: Runner())
    run = ResolvedRun(
        backend="api",
        max_turns=None,
        transcript_dir=None,
        require_executed=True,
        max_budget_usd=2,
        effort="high",
        parallel=1,
        output_format="text",
        judge=False,
        transcript_html=None,
        summary_md=markdown_path,
        summary_json=summary_path,
        trials=1,
        require="any",
        models=None,
        persist=False,
        baseline=False,
        gate_regressions=False,
        gate_cost_regression=False,
        verdict_cache=ledger_path,
    )
    dispatch_resolved_run([spec], run, grader=None, escalation=None)
    first = json.loads(summary_path.read_text())
    assert calls == ["one"]
    assert first["scenarios"][0]["content_key"]
    verdicts = VerdictLedger.load(ledger_path)
    verdicts.record([first], run_url="local")
    verdicts.write()
    calls.clear()
    dispatch_resolved_run([spec], run, grader=None, escalation=None)
    second = json.loads(summary_path.read_text())
    assert calls == []
    assert second["scenarios"] == []
    assert second["totals"]["cached"] == 1
    assert second["cached"][0]["run_url"] == "local"
    assert "| one | clean_room | cached pass |" in markdown_path.read_text()
    capsys.readouterr()
    dispatch_resolved_run([spec], dataclasses.replace(run, output_format="json"), grader=None, escalation=None)
    json_output = json.loads(capsys.readouterr().out)
    assert list(json_output) == ["scenarios", "summary", "cached"]
    assert json_output["cached"][0]["name"] == "one"
    dispatch_resolved_run([spec], dataclasses.replace(run, output_format="html"), grader=None, escalation=None)
    html_output = capsys.readouterr().out
    assert html_output.lstrip().lower().startswith("<!doctype html>")
    assert html_output.endswith("</html>\n")
    assert "CACHED PASS" not in html_output.split("</html>", 1)[1]
    assert "one" in html_output
    with patch("teatree.cli.eval.single_trial.finalize_single_run", return_value=False) as finalize:
        dispatch_resolved_run(
            [spec, dataclasses.replace(spec, name="two")],
            dataclasses.replace(run, persist=True),
            grader=None,
            escalation=None,
        )
    assert [item.name for item in finalize.call_args.kwargs["cached_specs"]] == ["one"]
    assert [item.name for item in finalize.call_args.kwargs["specs"]] == ["two"]
    assert finalize.call_args.kwargs["persist"] is True


def test_cli_records_summary_pass(tmp_path: Path) -> None:
    summary = tmp_path / "eval-one.json"
    summary.write_text(
        json.dumps(
            {
                "head_sha": "abc",
                "trial_count": 1,
                "scenarios": [
                    {
                        "name": "one",
                        "lane": "clean_room",
                        "content_key": "a" * 64,
                        "outcome": "PASS",
                        "cache_eligible": True,
                    },
                ],
            }
        )
    )
    ledger = tmp_path / "verdicts.json"
    result = CliRunner().invoke(
        app, ["eval", "verdict-cache", "record", str(ledger), str(summary), "--run-url", "https://example.test/run/1"]
    )
    assert result.exit_code == 0, result.output
    assert VerdictLedger.load(ledger).lookup("a" * 64) is not None


def test_cli_record_rejects_absent_explicit_summary_path(tmp_path: Path) -> None:
    ledger = tmp_path / "verdicts.json"
    result = CliRunner().invoke(
        app, ["eval", "verdict-cache", "record", str(ledger), str(tmp_path / "missing"), "--run-url", "local"]
    )
    assert result.exit_code == 2
    assert "cannot read eval summary" in result.output


def test_cli_merges_main_and_pr_ledgers(tmp_path: Path) -> None:
    target = tmp_path / "eval-verdicts.json"
    source = tmp_path / "eval-verdicts-main.json"
    now = datetime.now(UTC).isoformat()
    VerdictLedger(
        target,
        {"a" * 64: {"name": "pr", "lane": "clean_room", "run_url": "pr", "recorded_at": now, "head_sha": "pr"}},
    ).write()
    VerdictLedger(
        source,
        {"b" * 64: {"name": "main", "lane": "clean_room", "run_url": "main", "recorded_at": now, "head_sha": "main"}},
    ).write()
    result = CliRunner().invoke(app, ["eval", "verdict-cache", "merge", str(target), str(source)])
    assert result.exit_code == 0, result.output
    assert set(VerdictLedger.load(target).entries) == {"a" * 64, "b" * 64}


@pytest.mark.parametrize(
    "options",
    [
        ["--trials", "2"],
        ["--models", "claude-sonnet-4-6,claude-opus-4-6"],
        ["--model", "claude-sonnet-4-6"],
        ["--benchmark"],
        ["--backend", "transcript"],
        ["--baseline"],
        ["--gate-regressions"],
        ["--gate-cost-regression"],
        ["--gate-cost-bounds"],
    ],
)
def test_cli_rejects_incompatible_verdict_cache_options(tmp_path: Path, options: list[str]) -> None:
    result = CliRunner().invoke(
        app, ["eval", "run", "--backend", "api", *options, "--verdict-cache", str(tmp_path / "ledger.json")]
    )
    assert result.exit_code == 2
    assert "--verdict-cache requires a fresh single-trial run" in result.output
