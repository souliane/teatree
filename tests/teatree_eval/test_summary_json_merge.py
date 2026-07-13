"""Merge per-shard publish-safe ``--summary-json`` artifacts into one (§2.4).

The CI heal workflow shards the full suite across a parallel matrix; each shard
uploads its own publish-safe per-scenario ``--summary-json``. The merge folds them
into ONE ``eval-heal-<sha>`` JSON with the identical §2.4 schema — totals summed,
scenarios concatenated — so the ``t3 eval ci-status`` download path reads the
combined run exactly as it read a single-invocation run. ``head_sha`` /
``generated_at`` are injected (never computed here), so the merge is deterministic.
"""

import json
from pathlib import Path

from teatree.eval.summary_json_merge import merge_summary_json, merge_summary_payloads, summary_json_files

_SHA = "0123456789abcdef0123456789abcdef01234567"
_AT = "2026-07-13T06:00:00Z"

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
    "totals": {"total": 2, "passed": 1, "failed": 0, "skipped": 1},
    "scenarios": [
        {"name": "beta", "lane": "clean_room", "verdict": "pass", "triage_class": None},
        {"name": "gamma", "lane": "clean_room", "verdict": "skip", "triage_class": "no_coverage"},
    ],
}


class TestMergePayloads:
    def test_totals_are_summed_across_shards(self) -> None:
        merged = merge_summary_payloads([_SHARD_A, _SHARD_B], head_sha=_SHA, generated_at=_AT)
        assert merged["totals"] == {"total": 4, "passed": 2, "failed": 1, "skipped": 1}

    def test_scenarios_are_concatenated_across_shards(self) -> None:
        merged = merge_summary_payloads([_SHARD_A, _SHARD_B], head_sha=_SHA, generated_at=_AT)
        names = [scenario["name"] for scenario in merged["scenarios"]]
        assert names == ["zeta", "alpha", "beta", "gamma"]

    def test_injected_sha_and_timestamp_win_over_the_per_shard_values(self) -> None:
        merged = merge_summary_payloads([_SHARD_A, _SHARD_B], head_sha=_SHA, generated_at=_AT)
        assert merged["head_sha"] == _SHA
        assert merged["generated_at"] == _AT

    def test_a_single_model_survives_the_merge(self) -> None:
        merged = merge_summary_payloads([_SHARD_A, _SHARD_B], head_sha=_SHA, generated_at=_AT)
        assert merged["model"] == "claude-sonnet-4-6"

    def test_distinct_models_are_joined_deterministically(self) -> None:
        other = {**_SHARD_B, "model": "claude-opus-4-8"}
        merged = merge_summary_payloads([_SHARD_A, other], head_sha=_SHA, generated_at=_AT)
        assert merged["model"] == "claude-opus-4-8,claude-sonnet-4-6"

    def test_empty_input_yields_a_valid_empty_payload(self) -> None:
        merged = merge_summary_payloads([], head_sha=_SHA, generated_at=_AT)
        assert merged["totals"] == {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        assert merged["scenarios"] == []
        assert merged["model"] == "unknown"

    def test_no_transcript_key_can_enter_the_merge(self) -> None:
        # The merge only forwards the already-sanitized per-shard rows; it never
        # invents a transcript key, so the combined artifact stays publish-safe.
        merged = merge_summary_payloads([_SHARD_A, _SHARD_B], head_sha=_SHA, generated_at=_AT)
        blob = json.dumps(merged)
        for banned in ("text_blocks", "tool_calls", "rationale", "raw_stdout"):
            assert banned not in blob


class TestFileReader:
    def _write(self, root: Path) -> Path:
        shard_dir = root / "heal"
        shard_dir.mkdir()
        (shard_dir / "eval-heal-shard-1-2.json").write_text(json.dumps(_SHARD_A), encoding="utf-8")
        (shard_dir / "eval-heal-shard-2-2.json").write_text(json.dumps(_SHARD_B), encoding="utf-8")
        return shard_dir

    def test_reads_every_json_in_a_directory(self, tmp_path: Path) -> None:
        shard_dir = self._write(tmp_path)
        paths = summary_json_files([str(shard_dir)])
        assert [p.name for p in paths] == ["eval-heal-shard-1-2.json", "eval-heal-shard-2-2.json"]

    def test_merge_summary_json_reads_a_directory_and_renders_the_string(self, tmp_path: Path) -> None:
        shard_dir = self._write(tmp_path)
        rendered = merge_summary_json([str(shard_dir)], head_sha=_SHA, generated_at=_AT)
        merged = json.loads(rendered)
        assert merged["totals"]["total"] == 4
        assert {scenario["name"] for scenario in merged["scenarios"]} == {"zeta", "alpha", "beta", "gamma"}

    def test_explicit_file_paths_are_read(self, tmp_path: Path) -> None:
        shard_dir = self._write(tmp_path)
        one = shard_dir / "eval-heal-shard-1-2.json"
        rendered = merge_summary_json([str(one)], head_sha=_SHA, generated_at=_AT)
        assert json.loads(rendered)["totals"]["total"] == 2
