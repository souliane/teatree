"""``t3 eval merge-summary-json`` combines per-shard eval-heal JSONs into one.

The reusable subcommand reads N per-shard publish-safe ``--summary-json`` files
and emits ONE §2.4 JSON — totals summed, scenarios concatenated, ``--sha`` /
``--generated-at`` injected (never computed here, so the merge is deterministic).
Exercised through the real typer CLI so the workflow's combine-job invocation is
covered end to end.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from teatree.cli import app

_SHA = "0123456789abcdef0123456789abcdef01234567"
_META = ["--sha", _SHA, "--generated-at", "2026-07-13T06:00:00Z"]

_SHARD_A = {
    "generated_at": "2026-07-13T05:00:00Z",
    "model": "claude-sonnet-4-6",
    "head_sha": _SHA,
    "totals": {"total": 2, "passed": 1, "failed": 1, "skipped": 0},
    "scenarios": [
        {"name": "zeta", "lane": "clean_room", "verdict": "pass", "triage_class": None},
        {"name": "alpha", "lane": "under_load", "verdict": "fail", "triage_class": "behavioral"},
    ],
}
_SHARD_B = {
    "generated_at": "2026-07-13T05:30:00Z",
    "model": "claude-sonnet-4-6",
    "head_sha": _SHA,
    "totals": {"total": 1, "passed": 1, "failed": 0, "skipped": 0},
    "scenarios": [{"name": "beta", "lane": "clean_room", "verdict": "pass", "triage_class": None}],
}


def _write_shards(tmp_path: Path) -> Path:
    shard_dir = tmp_path / "heal"
    shard_dir.mkdir()
    (shard_dir / "eval-heal-shard-1-2.json").write_text(json.dumps(_SHARD_A), encoding="utf-8")
    (shard_dir / "eval-heal-shard-2-2.json").write_text(json.dumps(_SHARD_B), encoding="utf-8")
    return shard_dir


class TestMergeSummaryJson:
    def test_merges_a_directory_to_one_payload_with_summed_totals(self, tmp_path: Path) -> None:
        shard_dir = _write_shards(tmp_path)
        result = CliRunner().invoke(app, ["eval", "merge-summary-json", str(shard_dir), *_META])
        assert result.exit_code == 0, result.output
        merged = json.loads(result.output)
        assert merged["totals"] == {"total": 3, "passed": 2, "failed": 1, "skipped": 0}
        assert {scenario["name"] for scenario in merged["scenarios"]} == {"zeta", "alpha", "beta"}

    def test_injected_sha_and_timestamp_are_written(self, tmp_path: Path) -> None:
        shard_dir = _write_shards(tmp_path)
        result = CliRunner().invoke(app, ["eval", "merge-summary-json", str(shard_dir), *_META])
        merged = json.loads(result.output)
        assert merged["head_sha"] == _SHA
        assert merged["generated_at"] == "2026-07-13T06:00:00Z"

    def test_out_flag_writes_the_merged_file(self, tmp_path: Path) -> None:
        shard_dir = _write_shards(tmp_path)
        out_path = tmp_path / f"eval-heal-{_SHA}.json"
        result = CliRunner().invoke(app, ["eval", "merge-summary-json", str(shard_dir), *_META, "--out", str(out_path)])
        assert result.exit_code == 0, result.output
        merged = json.loads(out_path.read_text(encoding="utf-8"))
        assert merged["totals"]["total"] == 3

    def test_empty_dir_yields_a_valid_empty_payload(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = CliRunner().invoke(app, ["eval", "merge-summary-json", str(empty), *_META])
        assert result.exit_code == 0, result.output
        merged = json.loads(result.output)
        assert merged["totals"]["total"] == 0
        assert merged["scenarios"] == []
