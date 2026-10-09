"""Merge + drift-gate for the scheduled shard-durations refresh (#3160, #4603).

Unit arm: the pure merge/prune/decision logic. Integration arm: the ``main`` CLI over real
files, asserting it writes the merged file only when the refresh gate fires and emits
the ``refresh=`` verdict to ``$GITHUB_OUTPUT`` exactly as the workflow reads it.

#4603 is the reason a leg that delivered nothing is no longer fatal. Recording is a
measurement, not a verdict, so the refresh must survive a red lane — that is precisely when
the durations are most stale. What must NEVER happen is a *truncated* file, so the fresh
slices are layered OVER the committed baseline rather than replacing it: a leg that died
part-way through its group cannot remove the tests it never reached.
"""

import json
from pathlib import Path

import pytest

from scripts.ci.durations_refresh import decide_refresh, load_durations, main, merge_durations, prune_absent_files


def _write(path: Path, data: dict[str, float]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _touch(root: Path, relative: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")


class TestMergeDurations:
    def test_unions_disjoint_shard_slices(self, tmp_path: Path) -> None:
        a = _write(tmp_path / "a.json", {"tests/x.py::t1": 1.0, "tests/x.py::t2": 2.0})
        b = _write(tmp_path / "b.json", {"tests/y.py::t3": 3.0})
        merge = merge_durations([a, b], baseline={})
        assert merge.fresh == {
            "tests/x.py::t1": 1.0,
            "tests/x.py::t2": 2.0,
            "tests/y.py::t3": 3.0,
        }
        assert not merge.missing
        assert not merge.unrecorded

    def test_an_absent_artifact_is_named_not_fatal(self, tmp_path: Path) -> None:
        # #4603: a leg that failed or was cancelled uploads nothing. Its slice is missing,
        # not zero — the caller keeps those tests' committed timings instead of dropping them.
        present = _write(tmp_path / "present.json", {"tests/x.py::t1": 1.0})
        absent = tmp_path / "absent.json"
        merge = merge_durations([present, absent], baseline={})
        assert merge.fresh == {"tests/x.py::t1": 1.0}
        assert merge.missing == [absent]

    def test_an_artifact_equal_to_the_baseline_contributes_nothing(self, tmp_path: Path) -> None:
        # A cancelled leg dies before pytest rewrites the file, so its artifact is the
        # CHECKED-OUT committed file. Merging it would feed month-old timings back in as
        # if freshly measured — and, applied last, would overwrite a live leg's fresh ones.
        baseline = {"tests/x.py::t1": 1.0, "tests/y.py::t2": 9.0}
        stale = _write(tmp_path / "stale.json", dict(baseline))
        fresh = _write(tmp_path / "fresh.json", {"tests/x.py::t1": 4.0})
        merge = merge_durations([fresh, stale], baseline=baseline)
        assert merge.fresh == {"tests/x.py::t1": 4.0}
        assert merge.unrecorded == [stale]

    def test_load_reads_floats(self, tmp_path: Path) -> None:
        assert load_durations(_write(tmp_path / "d.json", {"t": 4})) == {"t": 4.0}


class TestPruneAbsentFiles:
    def test_drops_node_ids_whose_file_is_gone(self, tmp_path: Path) -> None:
        _touch(tmp_path, "tests/live.py")
        kept, dropped = prune_absent_files(
            {"tests/live.py::t1": 1.0, "tests/deleted.py::t2": 2.0},
            root=tmp_path,
        )
        assert kept == {"tests/live.py::t1": 1.0}
        assert dropped == ["tests/deleted.py::t2"]

    def test_keeps_doctest_items_whose_module_still_exists(self, tmp_path: Path) -> None:
        _touch(tmp_path, "src/teatree/thing.py")
        kept, dropped = prune_absent_files({"src/teatree/thing.py::teatree.thing.f": 1.0}, root=tmp_path)
        assert kept == {"src/teatree/thing.py::teatree.thing.f": 1.0}
        assert not dropped


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
        _touch(tmp_path, "tests/x.py")
        durations = _write(tmp_path / ".test_durations", {"tests/x.py::t1": 1.0})
        shard = _write(tmp_path / "shard-1.json", {"tests/x.py::t1": 1.0, "tests/x.py::t2": 2.0})
        output = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        rc = main(["--root", str(tmp_path), str(durations), str(shard)])
        assert rc == 0
        assert "refresh=true" in output.read_text(encoding="utf-8")
        assert json.loads(durations.read_text(encoding="utf-8")) == {"tests/x.py::t1": 1.0, "tests/x.py::t2": 2.0}

    def test_leaves_file_untouched_and_signals_false_within_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _touch(tmp_path, "tests/x.py")
        durations = _write(tmp_path / ".test_durations", {"tests/x.py::t1": 1.0})
        shard = _write(tmp_path / "shard-1.json", {"tests/x.py::t1": 1.02})
        output = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        rc = main(["--root", str(tmp_path), str(durations), str(shard)])
        assert rc == 0
        assert "refresh=false" in output.read_text(encoding="utf-8")
        # Untouched: the committed file is preserved when the gate does not fire.
        assert json.loads(durations.read_text(encoding="utf-8")) == {"tests/x.py::t1": 1.0}

    def test_usage_error_without_shard_paths(self) -> None:
        assert main([]) == 2

    def test_a_partial_lane_still_refreshes_and_keeps_the_absent_legs_tests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # THE #4603 acceptance case: one leg failed or was cancelled. The refresh must still
        # produce a file, it must carry the surviving leg's fresh timings, and the absent
        # leg's tests must keep their committed ones rather than vanishing from the split.
        _touch(tmp_path, "tests/x.py")
        _touch(tmp_path, "tests/y.py")
        durations = _write(tmp_path / ".test_durations", {"tests/x.py::t1": 1.0, "tests/y.py::t2": 5.0})
        present = _write(tmp_path / "shard-1.json", {"tests/x.py::t1": 1.0, "tests/x.py::t3": 8.0})
        absent = tmp_path / "shard-2.json"
        output = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        rc = main(["--root", str(tmp_path), str(durations), str(present), str(absent)])
        assert rc == 0
        assert "refresh=true" in output.read_text(encoding="utf-8")
        assert json.loads(durations.read_text(encoding="utf-8")) == {
            "tests/x.py::t1": 1.0,
            "tests/x.py::t3": 8.0,
            "tests/y.py::t2": 5.0,
        }
        assert "shard-2.json" in capsys.readouterr().err, "the leg that delivered nothing must be NAMED."

    def test_a_truncated_leg_never_removes_the_tests_it_did_not_reach(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A leg that FAILED mid-group uploads a real but incomplete slice. Replacing the
        # committed file with the union would drop the tests it never reached — the very
        # imbalance the refresh exists to cure.
        _touch(tmp_path, "tests/x.py")
        durations = _write(tmp_path / ".test_durations", {"tests/x.py::t1": 1.0, "tests/x.py::t2": 40.0})
        shard = _write(tmp_path / "shard-1.json", {"tests/x.py::t1": 10.0})
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_output"))
        assert main(["--root", str(tmp_path), str(durations), str(shard)]) == 0
        assert json.loads(durations.read_text(encoding="utf-8")) == {
            "tests/x.py::t1": 10.0,
            "tests/x.py::t2": 40.0,
        }

    def test_keys_naming_a_deleted_file_are_pruned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # 76 recorded keys named files that no longer exist. They survive the layering above
        # (the baseline is preserved wholesale), so the tree itself is the authority that drops them.
        _touch(tmp_path, "tests/x.py")
        durations = _write(tmp_path / ".test_durations", {"tests/x.py::t1": 1.0, "tests/gone.py::t2": 2.0})
        shard = _write(tmp_path / "shard-1.json", {"tests/x.py::t1": 1.0})
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_output"))
        assert main(["--root", str(tmp_path), str(durations), str(shard)]) == 0
        assert json.loads(durations.read_text(encoding="utf-8")) == {"tests/x.py::t1": 1.0}

    def test_an_unresolvable_root_skips_the_prune_rather_than_emptying_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The prune resolves node ids against the cwd. If that is ever not the repo root, every
        # key looks deleted — and an emptied durations file blinds the split completely, which is
        # far worse than the staleness being fixed here.
        durations = _write(tmp_path / ".test_durations", {"tests/x.py::t1": 1.0})
        shard = _write(tmp_path / "shard-1.json", {"tests/x.py::t1": 1.0, "tests/x.py::t2": 2.0})
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_output"))
        assert main(["--root", str(tmp_path / "not-the-repo"), str(durations), str(shard)]) == 0
        assert json.loads(durations.read_text(encoding="utf-8")) == {"tests/x.py::t1": 1.0, "tests/x.py::t2": 2.0}
        assert "unresolvable" in capsys.readouterr().err

    def test_no_leg_recording_anything_fails_loud_and_leaves_the_file_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The case the hard error was really guarding (#4584): the upload itself broke, so
        # NOTHING was measured. Publishing the committed file back as a "refresh" would read
        # as a clean no-op forever. Red the job, emit no verdict, touch nothing.
        _touch(tmp_path, "tests/x.py")
        durations = _write(tmp_path / ".test_durations", {"tests/x.py::t1": 1.0})
        output = tmp_path / "gh_output"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output))
        rc = main(["--root", str(tmp_path), str(durations), str(tmp_path / "shard-1.json")])
        assert rc == 1
        assert json.loads(durations.read_text(encoding="utf-8")) == {"tests/x.py::t1": 1.0}
        assert "shard-1.json" in capsys.readouterr().err
        assert not output.exists() or "refresh=" not in output.read_text(encoding="utf-8")
