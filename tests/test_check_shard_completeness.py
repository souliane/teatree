"""Anti-vacuity guard for the CI shard partition checker.

``scripts/ci/check_shard_completeness.py`` is the load-bearing proof that the
12-way ``test-shard`` matrix runs every test exactly once before the combiner
enforces the whole-tree 93% floor. If it could pass on a dropped or duplicated
shard, the required ``test (3.13)`` gate would go green while part of the suite
never ran. These tests pin that it fails LOUD on every such class and is GREEN
only on an exact partition.
"""

import json
from pathlib import Path

from scripts.ci.check_shard_completeness import evaluate, main


def _write(path: Path, *, total: int, selected: int, group: int | None, splits: int | None = 4) -> Path:
    path.write_text(
        json.dumps({"total_collected": total, "selected": selected, "group": group, "splits": splits}),
        encoding="utf-8",
    )
    return path


def _partition(tmp_path: Path, selected: list[int], *, total: int | None = None) -> list[Path]:
    total = sum(selected) if total is None else total
    return [
        _write(tmp_path / f"shard-stats.{i}.json", total=total, selected=n, group=i, splits=len(selected))
        for i, n in enumerate(selected, start=1)
    ]


class TestExactPartitionIsGreen:
    def test_exact_partition_passes(self, tmp_path: Path) -> None:
        paths = _partition(tmp_path, [25, 25, 25, 25])
        problems, total = evaluate(paths)
        assert problems == []
        assert total == 100

    def test_main_exit_zero_on_exact_partition(self, tmp_path: Path) -> None:
        paths = _partition(tmp_path, [10, 20, 30, 40])
        assert main([str(p) for p in paths]) == 0


class TestDroppedTestsAreRed:
    def test_sum_below_total_fails(self, tmp_path: Path) -> None:
        # Four shards agree the suite has 100 tests, but one silently selected 0.
        paths = _partition(tmp_path, [25, 25, 25, 0], total=100)
        problems, total = evaluate(paths)
        assert total is None
        assert any("sum to 75" in p and "100" in p for p in problems)

    def test_main_exit_one_on_dropped_shard(self, tmp_path: Path) -> None:
        paths = _partition(tmp_path, [25, 25, 25, 0], total=100)
        assert main([str(p) for p in paths]) == 1


class TestDuplicatedGroupIsRed:
    def test_sum_above_total_fails(self, tmp_path: Path) -> None:
        # A duplicated group: two shards ran group slices that overlap, so the
        # selected counts sum to MORE than the agreed total.
        paths = _partition(tmp_path, [40, 40, 40, 40], total=100)
        problems, _ = evaluate(paths)
        assert any("sum to 160" in p and "100" in p for p in problems)

    def test_repeated_group_index_flagged(self, tmp_path: Path) -> None:
        paths = [
            _write(tmp_path / "shard-stats.1.json", total=100, selected=50, group=1),
            _write(tmp_path / "shard-stats.2.json", total=100, selected=50, group=1),
        ]
        problems, _ = evaluate(paths)
        assert any("duplicate group index" in p for p in problems)


class TestDisagreementAndMissing:
    def test_shards_disagreeing_on_total_fail(self, tmp_path: Path) -> None:
        paths = [
            _write(tmp_path / "shard-stats.1.json", total=100, selected=50, group=1),
            _write(tmp_path / "shard-stats.2.json", total=99, selected=50, group=2),
        ]
        problems, total = evaluate(paths)
        assert total is None
        assert any("disagree on total collected" in p for p in problems)

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        present = _write(tmp_path / "shard-stats.1.json", total=100, selected=100, group=1)
        missing = tmp_path / "shard-stats.2.json"
        problems, _ = evaluate([present, missing])
        assert any("missing" in p and "shard-stats.2.json" in p for p in problems)

    def test_main_exit_one_when_a_shard_is_missing(self, tmp_path: Path) -> None:
        present = _write(tmp_path / "shard-stats.1.json", total=100, selected=100, group=1)
        assert main([str(present), str(tmp_path / "gone.json")]) == 1

    def test_malformed_json_fails(self, tmp_path: Path) -> None:
        bad = tmp_path / "shard-stats.1.json"
        bad.write_text("{not json", encoding="utf-8")
        problems, _ = evaluate([bad])
        assert any("unreadable" in p for p in problems)

    def test_no_paths_is_misuse(self) -> None:
        assert main([]) == 2
