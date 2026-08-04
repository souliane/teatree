"""The combiner's suite-wide reading of what the lane's raised ceiling bought (#4048).

Every shard records its slowest tests against the ceiling that applied to them.
This merges the twelve and names the ones running out of room — a report, never a
gate: the required context must not red for a test drifting toward a ceiling,
which is the class of red this epic removes.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.report_timeout_headroom import collect, main

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _shard(tmp_path: Path, group: int, entries: list[dict]) -> Path:
    path = tmp_path / f"shard-stats.{group}.json"
    path.write_text(
        json.dumps({"total_collected": 10, "selected": 5, "group": group, "slowest_against_ceiling": entries}),
        encoding="utf-8",
    )
    return path


def _entry(node_id: str, seconds: float, ceiling: float) -> dict:
    return {"node_id": node_id, "seconds": seconds, "ceiling": ceiling}


class TestReportTimeoutHeadroom:
    def test_room_to_spare_across_every_shard_reports_clean(self, tmp_path: Path, capsys) -> None:
        paths = [_shard(tmp_path, group, [_entry(f"tests/test_{group}.py::test_x", 2.0, 180.0)]) for group in (1, 2)]
        assert main([str(path) for path in paths]) == 0
        assert "none" in capsys.readouterr().out.lower()

    def test_a_test_running_out_of_room_is_named_with_its_ceiling(self, tmp_path: Path, capsys) -> None:
        paths = [
            _shard(tmp_path, 1, [_entry("tests/test_a.py::test_fine", 2.0, 180.0)]),
            _shard(tmp_path, 2, [_entry("tests/test_b.py::test_tight", 170.0, 180.0)]),
        ]
        assert main([str(path) for path in paths]) == 0
        out = capsys.readouterr().out
        assert "tests/test_b.py::test_tight" in out
        assert "tests/test_a.py::test_fine" not in out
        assert "180" in out

    def test_the_worst_offender_leads(self, tmp_path: Path, capsys) -> None:
        path = _shard(
            tmp_path,
            1,
            [_entry("tests/test_a.py::test_a", 140.0, 180.0), _entry("tests/test_b.py::test_b", 175.0, 180.0)],
        )
        assert main([str(path)]) == 0
        out = capsys.readouterr().out
        assert out.index("tests/test_b.py::test_b") < out.index("tests/test_a.py::test_a")

    def test_it_never_gates_even_on_a_recorded_over_run(self, tmp_path: Path, capsys) -> None:
        """A required check that reds for something no author caused is the failure this removes."""
        path = _shard(tmp_path, 1, [_entry("tests/test_a.py::test_over", 181.0, 180.0)])
        assert main([str(path)]) == 0
        assert "tests/test_a.py::test_over" in capsys.readouterr().out

    def test_a_shard_that_recorded_no_headroom_is_skipped_not_fatal(self, tmp_path: Path, capsys) -> None:
        older = tmp_path / "shard-stats.9.json"
        older.write_text(json.dumps({"total_collected": 10, "selected": 5, "group": 9}), encoding="utf-8")
        tight = _shard(tmp_path, 1, [_entry("tests/test_a.py::test_tight", 179.0, 180.0)])
        assert main([str(older), str(tight)]) == 0
        assert "tests/test_a.py::test_tight" in capsys.readouterr().out

    def test_a_missing_or_unreadable_file_is_reported_but_never_gates(self, tmp_path: Path, capsys) -> None:
        broken = tmp_path / "shard-stats.4.json"
        broken.write_text("{not json", encoding="utf-8")
        assert main([str(broken), str(tmp_path / "absent.json")]) == 0
        out = capsys.readouterr().out
        assert "shard-stats.4.json" in out
        assert "absent.json" in out

    def test_no_arguments_is_misuse(self, capsys) -> None:
        assert main([]) == 2


_TIGHT_SUITE = """
import time


def test_uses_most_of_its_ceiling():
    time.sleep(1.6)
"""


@pytest.mark.integration
def test_it_reads_what_the_plugin_actually_writes(tmp_path: Path) -> None:
    """Producer and consumer describe the same wire shape in two places — pin it end to end."""
    pytest.importorskip("pytest_timeout")
    (tmp_path / "test_tight.py").write_text(_TIGHT_SUITE, encoding="utf-8")
    stats_out = tmp_path / "shard-stats.1.json"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_tight.py",
            "-p",
            "scripts.ci.shard_stats_plugin",
            "-o",
            "timeout=2",
            f"--shard-stats-out={stats_out}",
            "-q",
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(_REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )

    pressured, unreadable = collect([stats_out])
    assert unreadable == []
    assert [pressure.node_id for pressure in pressured] == ["test_tight.py::test_uses_most_of_its_ceiling"]
    assert pressured[0].ceiling == pytest.approx(2.0)
