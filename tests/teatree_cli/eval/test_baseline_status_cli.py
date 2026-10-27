"""``t3 eval baseline-status`` through the CLI with a faked gh client (#4933).

The command reads the newest completed full-suite ``eval-ci-heal`` runs on a branch,
downloads each ``eval-heal-<sha>`` summary, and reports whether a recent green proof over
the CURRENT catalog is on record. Exit 0 only when it is; everything else exits 1.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.eval import baseline_status as baseline_status_mod
from teatree.eval.models import EvalSpec
from teatree.eval.summary_json import scenario_version
from teatree.utils.run import CommandFailedError

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=name,
        agent_path="skills/code/SKILL.md",
        prompt="run",
        matchers=(),
        source_path=Path("baseline-synthetic.yaml"),
    )


def _catalog(*names: str):
    return patch.object(baseline_status_mod, "discover_core_specs", return_value=[_spec(name) for name in names])


def _payload(*names: str, age: timedelta = timedelta(days=1), red: str | None = None, **extra: object) -> dict:
    rows = [
        {
            "name": name,
            "version": scenario_version(_spec(name)),
            "lane": "clean_room",
            "verdict": "fail" if name == red else "pass",
            "outcome": "BEHAVIOR_FAIL" if name == red else "PASS",
            "triage_class": "behavioral" if name == red else None,
        }
        for name in names
    ]
    failed = sum(row["verdict"] == "fail" for row in rows)
    return {
        "head_sha": _SHA,
        "generated_at": (datetime.now(UTC) - age).isoformat(),
        "totals": {"total": len(rows), "passed": len(rows) - failed, "failed": failed, "skipped": 0, "incomplete": 0},
        "scenarios": rows,
        **extra,
    }


class _FakeClient:
    def __init__(self, runs: list[dict[str, object]], payloads: dict[int, dict | str | Exception]) -> None:
        self.runs = runs
        self.payloads = payloads
        self.listed: list[tuple[str, str]] = []

    def list_runs(self, workflow: str, *, branch: str, limit: int = 20) -> list[dict[str, object]]:
        self.listed.append((workflow, branch))
        return self.runs

    def download_artifact(self, run_id: int | str, *, name: str, dest_dir: Path) -> None:
        payload = self.payloads[int(run_id)]
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (dest_dir / f"{name}.json").write_text(text, encoding="utf-8")


def _run(run_id: int, *, conclusion: str = "success", status: str = "completed") -> dict[str, object]:
    return {"databaseId": run_id, "headSha": _SHA, "status": status, "conclusion": conclusion}


def _invoke(client: _FakeClient, *args: str):
    with patch.object(baseline_status_mod, "build_ci_eval_client", return_value=client):
        return CliRunner().invoke(app, ["eval", "baseline-status", *args])


def test_no_full_suite_run_on_record_reports_missing_and_exits_nonzero() -> None:
    client = _FakeClient([], {})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 1, result.output
    assert "BASELINE MISSING" in result.output
    assert client.listed == [("eval-ci-heal.yml", "main")]


def test_a_recent_green_full_suite_run_exits_zero() -> None:
    client = _FakeClient([_run(11)], {11: _payload("a", "b")})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 0, result.output
    assert "BASELINE GREEN" in result.output


def test_a_proof_older_than_the_window_reports_stale() -> None:
    client = _FakeClient([_run(11)], {11: _payload("a", "b", age=timedelta(days=9))})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 1, result.output
    assert "BASELINE STALE" in result.output
    assert "9 day(s) old" in result.output


def test_the_window_can_be_widened() -> None:
    client = _FakeClient([_run(11)], {11: _payload("a", "b", age=timedelta(days=9))})

    with _catalog("a", "b"):
        result = _invoke(client, "--max-age-days", "14")

    assert result.exit_code == 0, result.output


def test_a_proof_that_covers_less_than_the_current_catalog_reports_stale() -> None:
    client = _FakeClient([_run(11)], {11: _payload("a", "b")})

    with _catalog("a", "b", "c"):
        result = _invoke(client)

    assert result.exit_code == 1, result.output
    assert "BASELINE STALE" in result.output
    assert "2 of 3" in result.output


def test_a_gating_red_in_the_newest_proof_reports_not_green() -> None:
    client = _FakeClient([_run(11, conclusion="failure")], {11: _payload("a", "b", red="b")})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 1, result.output
    assert "BASELINE NOT_GREEN" in result.output


def test_a_parked_newest_run_falls_back_to_the_older_proof() -> None:
    client = _FakeClient([_run(12), _run(11)], {12: _payload("a", "b", parked=True), 11: _payload("a", "b")})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 0, result.output


def test_a_red_run_is_judged_not_skipped_in_favour_of_an_older_green_one() -> None:
    runs = [_run(12, conclusion="failure"), _run(11)]
    client = _FakeClient(runs, {12: _payload("a", "b", red="b"), 11: _payload("a", "b")})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 1, result.output
    assert "BASELINE NOT_GREEN" in result.output


def test_a_run_still_in_progress_is_not_a_baseline() -> None:
    runs = [_run(13, status="in_progress", conclusion=""), _run(11)]
    client = _FakeClient(runs, {11: _payload("a", "b")})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 0, result.output


def test_a_cancelled_run_without_an_artifact_is_named_and_the_older_proof_still_counts() -> None:
    runs = [_run(12, conclusion="cancelled"), _run(11)]
    client = _FakeClient(runs, {12: CommandFailedError(["gh"], 1, "", "no artifact"), 11: _payload("a", "b")})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 0, result.output
    assert "run 12: could not download eval-heal-" in result.output


def test_an_unreadable_artifact_is_named_and_leaves_the_baseline_missing() -> None:
    client = _FakeClient([_run(11)], {11: CommandFailedError(["gh"], 1, "", "no artifact")})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 1, result.output
    assert "could not download eval-heal-" in result.output
    assert "BASELINE MISSING" in result.output


def test_a_corrupt_artifact_is_named_and_the_older_proof_still_counts() -> None:
    client = _FakeClient([_run(12), _run(11)], {12: "{not json", 11: _payload("a", "b")})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 0, result.output
    assert "run 12: eval-heal-" in result.output
    assert "Traceback" not in result.output


def test_a_corrupt_artifact_alone_leaves_the_baseline_missing() -> None:
    client = _FakeClient([_run(11)], {11: "{not json"})

    with _catalog("a", "b"):
        result = _invoke(client)

    assert result.exit_code == 1, result.output
    assert "BASELINE MISSING" in result.output


def test_a_listing_failure_is_reported_not_swallowed() -> None:
    class _Broken(_FakeClient):
        def list_runs(self, workflow: str, *, branch: str, limit: int = 20) -> list[dict[str, object]]:
            raise CommandFailedError(["gh"], 1, "", "bad credentials")

    with _catalog("a", "b"):
        result = _invoke(_Broken([], {}))

    assert result.exit_code == 1, result.output
    assert "BASELINE UNKNOWN" in result.output


@pytest.mark.parametrize("flag", ["--ref", "--repo"])
def test_ref_and_repo_are_options(flag: str) -> None:
    result = CliRunner().invoke(app, ["eval", "baseline-status", "--help"])

    assert flag in result.output
