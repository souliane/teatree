# test-path: cross-cutting
"""``scripts/freeze_phase_cost_baseline.py`` — the read-only freeze generator.

Lives beside the other ``scripts/`` tests rather than under a ``src/`` mirror dir:
it asserts the seam BETWEEN the script and ``teatree.core.cost_baseline`` — that
the generator's output parses under the loader that will read it.

Exercised against a synthetic control DB, so the assertions hold on any machine
and the live DB is never touched. The behaviours that matter are the two the
artifact's trustworthiness rests on: park-audit rows are excluded, and the
cutover filter makes a later run reproducible.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts.freeze_phase_cost_baseline import CUTOVER_MERGED_AT, render_baseline
from teatree.core.cost_baseline.frozen import load_frozen_baseline

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "freeze_phase_cost_baseline.py"
_CUTOVER = CUTOVER_MERGED_AT.replace("T", " ").rstrip("Z")


def _control_db(path: Path, attempts: list[dict[str, object]]) -> Path:
    connection = sqlite3.connect(str(path))
    connection.execute("CREATE TABLE teatree_task (id INTEGER PRIMARY KEY, phase TEXT)")
    connection.execute(
        "CREATE TABLE teatree_taskattempt ("
        "id INTEGER PRIMARY KEY, task_id INTEGER, model TEXT, input_tokens INTEGER, "
        "output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
        "num_turns INTEGER, cost_usd REAL, started_at TEXT)"
    )
    phases = {str(a["phase"]) for a in attempts}
    task_ids = {phase: index + 1 for index, phase in enumerate(sorted(phases))}
    for phase, task_id in task_ids.items():
        connection.execute("INSERT INTO teatree_task (id, phase) VALUES (?, ?)", (task_id, phase))
    for attempt in attempts:
        connection.execute(
            "INSERT INTO teatree_taskattempt (task_id, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, num_turns, cost_usd, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_ids[str(attempt["phase"])],
                attempt["model"],
                attempt.get("input_tokens"),
                attempt.get("output_tokens"),
                attempt.get("cache_read_tokens"),
                attempt.get("cache_write_tokens"),
                attempt.get("num_turns"),
                attempt.get("cost_usd"),
                attempt["started_at"],
            ),
        )
    connection.commit()
    connection.close()
    return path


def _real(phase: str, started_at: str, *, output_tokens: int = 100, cost_usd: float = 1.0) -> dict[str, object]:
    return {
        "phase": phase,
        "model": "claude-opus-4-8",
        "input_tokens": 10,
        "output_tokens": output_tokens,
        "cache_read_tokens": 1,
        "cache_write_tokens": 1,
        "num_turns": 4,
        "cost_usd": cost_usd,
        "started_at": started_at,
    }


def _park_row(phase: str, started_at: str) -> dict[str, object]:
    """A ``limit_parked:`` audit row: no model, no tokens, no cost — 99.6% of the table."""
    return {"phase": phase, "model": "", "started_at": started_at}


class TestRenderBaseline:
    def test_park_audit_rows_never_reach_the_aggregate(self, tmp_path: Path) -> None:
        db = _control_db(
            tmp_path / "control.sqlite3",
            [_real("coding", "2026-07-20 10:00:00")] + [_park_row("coding", "2026-07-20 10:00:01")] * 500,
        )
        document = yaml.safe_load(render_baseline(db, cutover_at=_CUTOVER))
        assert document["per_phase"]["coding"]["attempts"] == 1
        assert document["coverage"]["taskattempt_rows_total"] == 501
        assert document["coverage"]["real_dispatch_rows"] == 1

    def test_attempts_at_or_after_the_cutover_are_excluded_and_counted_separately(self, tmp_path: Path) -> None:
        db = _control_db(
            tmp_path / "control.sqlite3",
            [_real("coding", "2026-07-20 10:00:00"), _real("coding", "2026-07-26 10:00:00")],
        )
        document = yaml.safe_load(render_baseline(db, cutover_at=_CUTOVER))
        assert document["per_phase"]["coding"]["attempts"] == 1
        assert document["coverage"]["post_cutover_rows"] == 1

    def test_a_later_run_with_new_post_cutover_rows_reproduces_the_same_tables(self, tmp_path: Path) -> None:
        before = _control_db(tmp_path / "before.sqlite3", [_real("coding", "2026-07-20 10:00:00")])
        after = _control_db(
            tmp_path / "after.sqlite3",
            [_real("coding", "2026-07-20 10:00:00"), _real("coding", "2026-08-01 10:00:00", output_tokens=99_999)],
        )
        first = yaml.safe_load(render_baseline(before, cutover_at=_CUTOVER))
        second = yaml.safe_load(render_baseline(after, cutover_at=_CUTOVER))
        assert first["per_phase"] == second["per_phase"]
        assert first["per_phase_model"] == second["per_phase_model"]

    def test_the_rendered_document_round_trips_through_the_loader(self, tmp_path: Path) -> None:
        db = _control_db(tmp_path / "control.sqlite3", [_real("coding", "2026-07-20 10:00:00")])
        out = tmp_path / "baseline.yaml"
        out.write_text(render_baseline(db, cutover_at=_CUTOVER), encoding="utf-8")
        baseline = load_frozen_baseline(out)
        assert baseline.per_phase["coding"].attempts == 1
        assert baseline.predicates_agree

    def test_an_empty_window_refuses_rather_than_freezing_nothing(self, tmp_path: Path) -> None:
        db = _control_db(tmp_path / "control.sqlite3", [_real("coding", "2026-08-01 10:00:00")])
        with pytest.raises(SystemExit, match="refusing to freeze an empty baseline"):
            render_baseline(db, cutover_at=_CUTOVER)


class TestCheckMode:
    def test_check_reports_drift_against_a_stale_file(self, tmp_path: Path) -> None:
        db = _control_db(tmp_path / "control.sqlite3", [_real("coding", "2026-07-20 10:00:00")])
        stale = tmp_path / "baseline.yaml"
        stale.write_text("per_phase: {}\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db), "--out", str(stale), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "DRIFT" in result.stdout

    def test_write_then_check_agrees(self, tmp_path: Path) -> None:
        db = _control_db(tmp_path / "control.sqlite3", [_real("coding", "2026-07-20 10:00:00")])
        out = tmp_path / "baseline.yaml"
        written = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db), "--out", str(out)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert written.returncode == 0
        checked = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(db), "--out", str(out), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert checked.returncode == 0
