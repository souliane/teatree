"""``t3 eval quarantine`` — inspect, validate, and audit the known-red registry (#4173).

Exercised through the real typer CLI so the workflow invocations are covered end to end:
``check`` is the static validator (expired / unknown / malformed), ``audit`` reads a
merged eval-heal payload and is the loud channel the heal lane runs — it NAMES the
quarantined reds and reds on an entry that has become a lie.
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval.multi_trial import run_pass_at_k_lane
from teatree.eval.models import EvalRun, EvalSpec, Matcher
from teatree.eval.quarantine import load_quarantine

ISSUE = "https://github.com/souliane/teatree/issues/4172"
_SHA = "0123456789abcdef0123456789abcdef01234567"


def _registry(tmp_path: Path, *, until: str = "2999-01-01", scenario: str = "flaky_one") -> Path:
    path = tmp_path / "quarantine.yaml"
    path.write_text(
        f"scenarios:\n  {scenario}:\n    issue: {ISSUE}\n    until: {until}\n    reason: tracked known red\n",
        encoding="utf-8",
    )
    return path


def _payload(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    out = tmp_path / f"eval-heal-{_SHA}.json"
    passed = sum(1 for row in rows if row["triage_class"] is None)
    out.write_text(
        json.dumps(
            {
                "head_sha": _SHA,
                "totals": {"total": len(rows), "passed": passed, "failed": len(rows) - passed, "skipped": 0},
                "scenarios": rows,
            }
        ),
        encoding="utf-8",
    )
    return out


def _row(name: str, *, red: bool) -> dict[str, object]:
    return {"name": name, "lane": "clean_room", "triage_class": "behavioral" if red else None}


class _NoToolCallRunner:
    """A runner that makes no tool call, so a positive-matcher scenario fails."""

    def run(self, spec: EvalSpec) -> EvalRun:
        return EvalRun(
            spec_name=spec.name,
            tool_calls=(),
            text_blocks=("no tool call",),
            terminal_reason="end_turn",
            is_error=False,
            raw_stdout="",
            raw_stderr="",
            cost_usd=0.01,
        )


class TestList:
    def test_it_renders_each_entry_with_its_issue_and_expiry(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["eval", "quarantine", "list", "--file", str(_registry(tmp_path))])
        assert result.exit_code == 0, result.output
        assert "flaky_one" in result.output
        assert ISSUE in result.output
        assert "2999-01-01" in result.output

    def test_an_empty_registry_says_so(self, tmp_path: Path) -> None:
        empty = tmp_path / "quarantine.yaml"
        empty.write_text("scenarios: {}\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["eval", "quarantine", "list", "--file", str(empty)])
        assert result.exit_code == 0, result.output
        assert "no quarantined" in result.output.lower()


class TestCheck:
    def test_a_healthy_registry_exits_zero(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            app,
            ["eval", "quarantine", "check", "--file", str(_registry(tmp_path)), "--scenario", "flaky_one"],
        )
        assert result.exit_code == 0, result.output

    def test_an_expired_entry_reds(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            app,
            [
                "eval",
                "quarantine",
                "check",
                "--file",
                str(_registry(tmp_path, until="2000-01-01")),
                "--scenario",
                "flaky_one",
            ],
        )
        assert result.exit_code == 1, result.output
        assert "EXPIRED" in result.output

    def test_an_entry_naming_no_such_scenario_reds(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            app,
            ["eval", "quarantine", "check", "--file", str(_registry(tmp_path)), "--scenario", "something_else"],
        )
        assert result.exit_code == 1, result.output
        assert "UNKNOWN" in result.output

    def test_a_malformed_registry_reds_loud(self, tmp_path: Path) -> None:
        broken = tmp_path / "quarantine.yaml"
        broken.write_text("scenarios:\n  x:\n    reason: no issue, no date\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["eval", "quarantine", "check", "--file", str(broken)])
        assert result.exit_code == 1, result.output
        assert "issue" in result.output

    def test_the_shipped_registry_is_valid(self) -> None:
        result = CliRunner().invoke(app, ["eval", "quarantine", "check"])
        assert result.exit_code == 0, result.output


class TestAudit:
    def test_a_quarantined_red_is_named_and_does_not_red_the_audit(self, tmp_path: Path) -> None:
        # The weekly/heal lane's report: the red is expected and tracked, so the audit
        # NAMES it unmissably rather than adding a second failure on top of the run's own.
        payload = _payload(tmp_path, [_row("flaky_one", red=True), _row("other", red=False)])
        result = CliRunner().invoke(
            app, ["eval", "quarantine", "audit", str(payload), "--file", str(_registry(tmp_path))]
        )
        assert result.exit_code == 0, result.output
        assert "STILL RED flaky_one" in result.output
        assert ISSUE in result.output

    def test_a_quarantined_scenario_that_passed_reds_the_audit(self, tmp_path: Path) -> None:
        # The list has become a lie in the other direction — the entry must be deleted.
        payload = _payload(tmp_path, [_row("flaky_one", red=False)])
        result = CliRunner().invoke(
            app, ["eval", "quarantine", "audit", str(payload), "--file", str(_registry(tmp_path))]
        )
        assert result.exit_code == 1, result.output
        assert "ESCAPED flaky_one" in result.output

    def test_a_scenario_the_run_never_carried_is_reported_not_red(self, tmp_path: Path) -> None:
        payload = _payload(tmp_path, [_row("other", red=False)])
        result = CliRunner().invoke(
            app, ["eval", "quarantine", "audit", str(payload), "--file", str(_registry(tmp_path))]
        )
        assert result.exit_code == 0, result.output
        assert "NOT RUN flaky_one" in result.output

    def test_an_expired_entry_reds_the_audit_too(self, tmp_path: Path) -> None:
        payload = _payload(tmp_path, [_row("flaky_one", red=True)])
        result = CliRunner().invoke(
            app,
            ["eval", "quarantine", "audit", str(payload), "--file", str(_registry(tmp_path, until="2000-01-01"))],
        )
        assert result.exit_code == 1, result.output
        assert "EXPIRED" in result.output

    def test_a_missing_payload_reds(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["eval", "quarantine", "audit", str(tmp_path / "absent.json")])
        assert result.exit_code == 1, result.output

    def test_an_empty_quarantine_audits_clean(self, tmp_path: Path) -> None:
        empty = tmp_path / "quarantine.yaml"
        empty.write_text("scenarios: {}\n", encoding="utf-8")
        payload = _payload(tmp_path, [_row("other", red=True)])
        result = CliRunner().invoke(app, ["eval", "quarantine", "audit", str(payload), "--file", str(empty)])
        assert result.exit_code == 0, result.output


class TestQuarantineNeverReachesARunVerdict:
    """Quarantine is SELECTION-scope; the run verdict keeps the no-known-red-allowance rule.

    BLUEPRINT's under-load bullet says every scenario must be GREEN and a failing one reds
    the run outright. Suppressing a scenario from the bounded PR lane must not become a
    tolerated-red list: if the scenario RUNS, its failure still reds.
    """

    def test_a_quarantined_scenario_that_runs_red_still_reds_the_lane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        quarantine = load_quarantine(_registry(tmp_path, scenario="quarantined_red"))
        assert quarantine.suppressed() == frozenset({"quarantined_red"})

        spec = EvalSpec(
            name="quarantined_red",
            scenario="synthetic",
            agent_path="skills/rules/SKILL.md",
            prompt="do",
            matchers=(
                Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="never-emitted"),
            ),
            source_path=tmp_path / "spec.yaml",
            judge=None,
        )
        monkeypatch.setattr("teatree.cli.eval.multi_trial.make_runner", lambda *a, **k: _NoToolCallRunner())
        assert run_pass_at_k_lane(
            [spec],
            max_turns=None,
            trials=1,
            require="any",
            output_format="text",
            persist=False,
            model_override="claude-sonnet-4-6",
        )
