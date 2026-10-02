"""``t3 eval merge-summaries`` merges per-shard summaries into one dashboard.

The reusable overlay-facing CLI reads N per-shard sanitized ``--summary-md``
files and emits ONE combined dashboard: a title, an injected run line (run-url /
sha / generated-at — never computed here, so the merge is deterministic), summed
PASS/FAIL/skip totals, the merged per-scenario table sorted by lane then name,
and a final transcript-link line. These exercise the command through the real
typer CLI, so the parity with the host's ``scripts/eval/merge_summaries.py`` shim
is exercised end to end.
"""

from pathlib import Path

from typer.testing import CliRunner

from teatree.cli import app

_META = ["--run-url", "https://x/run/1", "--sha", "deadbeef", "--generated-at", "2026-06-24T06:00:00Z"]

_SHARD_A = """**1 passed**, **1 failed**, **0 skipped** (of 2) · model `claude-sonnet-4-6` · cost $0.5000

| scenario | lane | verdict | trials | cost |
| --- | --- | --- | --- | --- |
| zeta | clean_room | pass | 3/3 | $0.3000 |
| alpha | under_load | fail | 0/3 | $0.2000 |
"""

_SHARD_B = """**1 passed**, **0 failed**, **1 skipped** (of 2) · model `claude-sonnet-4-6` · cost $0.2500

| scenario | lane | verdict | trials | cost |
| --- | --- | --- | --- | --- |
| beta | clean_room | pass | 2/3 | $0.2500 |
| gamma | clean_room | skip | - | - |
"""


def _write_shards(tmp_path: Path) -> Path:
    shard_dir = tmp_path / "summaries"
    shard_dir.mkdir()
    (shard_dir / "eval-summary-clean_room-1-2.md").write_text(_SHARD_A, encoding="utf-8")
    (shard_dir / "eval-summary-clean_room-2-2.md").write_text(_SHARD_B, encoding="utf-8")
    return shard_dir


class TestMergeSummaries:
    def test_merges_to_one_table_with_summed_totals_and_cost(self, tmp_path: Path) -> None:
        shard_dir = _write_shards(tmp_path)
        result = CliRunner().invoke(app, ["eval", "merge-summaries", str(shard_dir), *_META])
        assert result.exit_code == 0, result.output
        for name in ("alpha", "beta", "gamma", "zeta"):
            assert name in result.output
        assert "2 passed" in result.output
        assert "1 failed" in result.output
        assert "1 skipped" in result.output
        assert "total cost $0.7500" in result.output

    def test_run_line_carries_injected_metadata(self, tmp_path: Path) -> None:
        shard_dir = _write_shards(tmp_path)
        result = CliRunner().invoke(app, ["eval", "merge-summaries", str(shard_dir), *_META])
        assert "https://x/run/1" in result.output
        assert "deadbeef" in result.output
        assert "2026-06-24T06:00:00Z" in result.output

    def test_rows_sorted_by_lane_then_name(self, tmp_path: Path) -> None:
        shard_dir = _write_shards(tmp_path)
        result = CliRunner().invoke(app, ["eval", "merge-summaries", str(shard_dir), *_META])
        order = [result.output.index(n) for n in ("beta", "gamma", "zeta", "alpha")]
        assert order == sorted(order)

    def test_out_flag_writes_file(self, tmp_path: Path) -> None:
        shard_dir = _write_shards(tmp_path)
        out_path = tmp_path / "index.md"
        result = CliRunner().invoke(app, ["eval", "merge-summaries", str(shard_dir), *_META, "--out", str(out_path)])
        assert result.exit_code == 0, result.output
        body = out_path.read_text(encoding="utf-8")
        assert "alpha" in body
        assert "2 passed" in body

    def test_empty_dir_still_emits_a_dashboard(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        result = CliRunner().invoke(app, ["eval", "merge-summaries", str(empty), *_META])
        assert result.exit_code == 0, result.output
        assert "0 passed" in result.output
