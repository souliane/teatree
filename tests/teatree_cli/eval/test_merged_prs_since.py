"""``t3 eval merged-prs-since`` is the reusable scheduled-eval pre-check.

The reusable overlay-facing CLI reads the repo's merged PRs from a JSON file and
answers whether ANY merged inside a lookback window. Exit 0 → run the eval; exit
``--skip-code`` → skip cleanly (nothing new); a non-list payload exits 2. These
exercise the command through the real typer CLI with a pinned ``--now``, so the
parity with the host's ``scripts/eval/merged_prs_since.py`` shim is exercised end
to end and no clock flake creeps in.
"""

import datetime as dt
import json
from pathlib import Path

from typer.testing import CliRunner

from teatree.cli import app

_NOW = dt.datetime(2026, 6, 7, 12, 0, tzinfo=dt.UTC).isoformat()


def _write_prs(tmp_path: Path, merged_at: str | None) -> Path:
    prs_file = tmp_path / "prs.json"
    prs_file.write_text(json.dumps([{"number": 1, "merged_at": merged_at}]), encoding="utf-8")
    return prs_file


class TestMergedPrsSince:
    def test_exit_zero_when_something_merged_in_window(self, tmp_path: Path) -> None:
        prs_file = _write_prs(tmp_path, "2026-06-06T09:00:00Z")
        result = CliRunner().invoke(
            app, ["eval", "merged-prs-since", "--prs-file", str(prs_file), "--days", "7", "--now", _NOW]
        )
        assert result.exit_code == 0, result.output
        assert "run the weekly eval" in result.output

    def test_exit_skip_code_when_nothing_merged_in_window(self, tmp_path: Path) -> None:
        prs_file = _write_prs(tmp_path, "2026-05-01T09:00:00Z")
        result = CliRunner().invoke(
            app,
            ["eval", "merged-prs-since", "--prs-file", str(prs_file), "--days", "7", "--skip-code", "1", "--now", _NOW],
        )
        assert result.exit_code == 1
        assert "skipping" in result.output

    def test_unmerged_pr_skips(self, tmp_path: Path) -> None:
        prs_file = _write_prs(tmp_path, None)
        result = CliRunner().invoke(
            app, ["eval", "merged-prs-since", "--prs-file", str(prs_file), "--days", "7", "--now", _NOW]
        )
        assert result.exit_code == 1

    def test_non_list_payload_exits_two(self, tmp_path: Path) -> None:
        prs_file = tmp_path / "prs.json"
        prs_file.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        result = CliRunner().invoke(
            app, ["eval", "merged-prs-since", "--prs-file", str(prs_file), "--days", "7", "--now", _NOW]
        )
        assert result.exit_code == 2
        assert "must contain a JSON list" in result.output
