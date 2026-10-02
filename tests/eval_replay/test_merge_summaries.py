"""Merge per-shard sanitized summaries into one weekly dashboard markdown.

The weekly metered workflow fans out across shards; each shard uploads its own
sanitized ``--summary-md`` artifact. ``merge_summaries.py`` reads those N
per-shard markdown files and emits ONE combined dashboard: a title, a run line
(run-url / sha / generated-at injected by the workflow — never computed here so
the script stays deterministic), summed PASS/FAIL/skip totals, the merged
per-scenario table (sorted by lane then name), and a final line linking the run
for the private transcript artifacts.
"""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "merge_summaries",
    Path(__file__).parents[2] / "scripts" / "eval" / "merge_summaries.py",
)
assert _SPEC is not None
assert _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)

main = _MOD.main

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
    def test_merges_to_one_table_with_summed_totals(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        shard_dir = _write_shards(tmp_path)
        code = main([str(shard_dir), *_META])
        out = capsys.readouterr().out
        assert code == 0
        for name in ("alpha", "beta", "gamma", "zeta"):
            assert name in out
        # Summed totals across both shards: 2 passed, 1 failed, 1 skipped.
        assert "2 passed" in out
        assert "1 failed" in out
        assert "1 skipped" in out
        # Per-scenario cost survives the merge and the dashboard sums it: 0.30+0.20+0.25 = 0.75.
        assert "$0.3000" in out
        assert "total cost $0.7500" in out

    def test_run_line_carries_injected_metadata(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        shard_dir = _write_shards(tmp_path)
        main([str(shard_dir), *_META])
        out = capsys.readouterr().out
        assert "https://x/run/1" in out
        assert "deadbeef" in out
        assert "2026-06-24T06:00:00Z" in out

    def test_rows_sorted_by_lane_then_name(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        shard_dir = _write_shards(tmp_path)
        main([str(shard_dir), *_META])
        out = capsys.readouterr().out
        # clean_room (beta, gamma, zeta) before under_load (alpha); within a lane, by name.
        order = [out.index(n) for n in ("beta", "gamma", "zeta", "alpha")]
        assert order == sorted(order)

    def test_out_flag_writes_file(self, tmp_path: Path) -> None:
        shard_dir = _write_shards(tmp_path)
        out_path = tmp_path / "index.md"
        code = main([str(shard_dir), *_META, "--out", str(out_path)])
        assert code == 0
        body = out_path.read_text(encoding="utf-8")
        assert "alpha" in body
        assert "2 passed" in body

    def test_empty_dir_still_emits_a_dashboard(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        code = main([str(empty), *_META])
        out = capsys.readouterr().out
        assert code == 0
        assert "0 passed" in out
