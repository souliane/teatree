"""Merge + drift-gate for the scheduled shard-durations refresh (#3160).

Unit arm: the pure merge/decision logic. Integration arm: the ``main`` CLI over real
files, asserting it writes the merged file only when the refresh gate fires and emits
the ``refresh=`` verdict to ``$GITHUB_OUTPUT`` exactly as the workflow reads it.
"""

import json
from pathlib import Path

import pytest

from scripts.ci.durations_refresh import decide_refresh, load_durations, main, merge_durations


def _write(path: Path, data: dict[str, float]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestMergeDurations:
    def test_unions_disjoint_shard_slices(self, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.json", {"tests/x.py::t1": 1.0, "tests/x.py::t2": 2.0})
        b = _write(tmp_path / "b.json", {"tests/y.py::t3": 3.0})
        assert merge_durations([a, b]) == {
            "tests/x.py::t1": 1.0,
            "tests/x.py::t2": 2.0,
            "tests/y.py::t3": 3.0,
        }

    def test_missing_file_contributes_nothing(self, tmp_path: Path) -> None:
        assert merge_durations([tmp_path / "absent.json"]) == {}

    def test_load_reads_floats(self, tmp_path: Path) -> None:
        assert load_durations(_write(tmp_path / "d.json", {"t": 4})) == {"t": 4.0}


class TestDecideRefresh:
    def test_refreshes_when_tests_added(self) -> None:
        decision = decide_refresh({"t1": 1.0}, {"t1": 1.0, "t2": 2.0})
        assert decision.should_refresh
        assert decision.added == 1
        assert "test set changed" in decision.reason

    def test_refreshes_when_tests_removed(self) -> None:
        decision = decide_refresh({"t1": 1.0, "gone": 9.0}, {"t1": 1.0})
        assert decision.should_refresh
        assert decision.removed == 1

    def test_refreshes_on_large_aggregate_drift(self) -> None:
        # Same test set, but the timings doubled -> 100% drift, well past 15%.
        decision = decide_refresh({"t1": 1.0, "t2": 1.0}, {"t1": 2.0, "t2": 2.0})
        assert decision.should_refresh
        assert decision.drift_ratio == pytest.approx(1.0)
        assert "duration drift" in decision.reason

    def test_holds_within_threshold(self) -> None:
        # 5% jitter, no set change -> no PR churn.
        decision = decide_refresh({"t1": 1.0}, {"t1": 1.05})
        assert not decision.should_refresh
        assert decision.drift_ratio == pytest.approx(0.05)

    def test_holds_on_identical_input(self) -> None:
        same = {"t1": 1.0, "t2": 2.0}
        assert not decide_refresh(same, dict(same)).should_refresh


class TestMainCli:
    def test_writes_and_signals_refresh_when_set_changed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        durations = _write(tmp_path / ".test_durations", {"t1": 1.0})
        shard = _write(tmp_path / "shard-1.json", {"t1": 1.0, "t2": 2.0})
        output = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        rc = main([str(durations), str(shard)])
        assert rc == 0
        assert "refresh=true" in output.read_text(encoding="utf-8")
        assert json.loads(durations.read_text(encoding="utf-8")) == {"t1": 1.0, "t2": 2.0}

    def test_leaves_file_untouched_and_signals_false_within_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        durations = _write(tmp_path / ".test_durations", {"t1": 1.0})
        shard = _write(tmp_path / "shard-1.json", {"t1": 1.02})
        output = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        rc = main([str(durations), str(shard)])
        assert rc == 0
        assert "refresh=false" in output.read_text(encoding="utf-8")
        # Untouched: the committed file is preserved when the gate does not fire.
        assert json.loads(durations.read_text(encoding="utf-8")) == {"t1": 1.0}

    def test_usage_error_without_shard_paths(self) -> None:
        assert main([]) == 2
