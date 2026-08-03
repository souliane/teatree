"""How much room recorded tests have left against their own ceilings (#4048)."""

import json
from pathlib import Path

import pytest

from teatree.quality.durations_file import DurationsUnreadableError
from teatree.quality.timeout_headroom import TIGHT_FRACTION, measure_timeout_headroom

_PYPROJECT = """
[tool.pytest.ini_options]
timeout = 60
"""


def _repo(tmp_path: Path, *, sources: dict[str, str], durations: dict[str, float], pyproject: str = _PYPROJECT) -> Path:
    (tmp_path / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    for rel, body in sources.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (tmp_path / "dev").mkdir(exist_ok=True)
    (tmp_path / "dev" / ".test_durations").write_text(json.dumps(durations), encoding="utf-8")
    return tmp_path


_PLAIN = """
def test_slow() -> None:
    pass
"""

_MARKED = """
import pytest


@pytest.mark.timeout(240)
def test_slow() -> None:
    pass
"""

_NAMED_CEILING = """
import pytest

BUDGET = 240


@pytest.mark.timeout(BUDGET)
def test_slow() -> None:
    pass
"""


class TestMeasureTimeoutHeadroom:
    def test_a_comfortable_test_is_not_reported(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _PLAIN}, durations={"tests/test_a.py::test_slow": 1.0})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.pressured == ()
        assert report.is_healthy

    def test_a_test_squeezing_its_ceiling_is_reported_but_still_healthy(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _PLAIN}, durations={"tests/test_a.py::test_slow": 56.85})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.pressured] == ["tests/test_a.py::test_slow"]
        assert report.pressured[0].ceiling == pytest.approx(60.0)
        assert report.pressured[0].consumed == pytest.approx(0.9475)
        assert report.pressured[0].is_over is False
        assert report.is_healthy

    def test_a_recorded_over_run_is_not_a_risk_but_a_fact(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _PLAIN}, durations={"tests/test_a.py::test_slow": 61.0})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.over_ceiling] == ["tests/test_a.py::test_slow"]
        assert not report.is_healthy

    def test_a_stated_marker_ceiling_beats_the_lane_ini(self, tmp_path: Path) -> None:
        """``pytest_timeout._get_item_settings`` prefers the marker, so the judge must too."""
        repo = _repo(tmp_path, sources={"tests/test_a.py": _MARKED}, durations={"tests/test_a.py::test_slow": 100.0})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.pressured == ()

    def test_a_marked_test_over_its_own_stated_ceiling_still_fails(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _MARKED}, durations={"tests/test_a.py::test_slow": 240.2})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.ceiling for pressure in report.over_ceiling] == [240.0]

    def test_a_ceiling_named_rather_than_written_is_skipped_never_guessed(self, tmp_path: Path) -> None:
        """An unresolvable marker is missing evidence — judging it against the ini would invent a verdict."""
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _NAMED_CEILING},
            durations={"tests/test_a.py::test_slow": 100.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.pressured == ()
        assert report.unresolved_ceilings == 1

    def test_a_recorded_key_whose_file_is_gone_is_skipped(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={}, durations={"tests/test_deleted.py::test_slow": 90.0})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.pressured == ()

    def test_a_recorded_key_whose_test_was_renamed_is_skipped(self, tmp_path: Path) -> None:
        """The live tree's worst recorded entry was one of these — 180.2s against a test that is gone."""
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _PLAIN},
            durations={"tests/test_a.py::test_under_its_old_name": 90.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.pressured == ()

    def test_a_parametrised_test_is_matched_to_its_function(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _PLAIN},
            durations={"tests/test_a.py::test_slow[case-1]": 58.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.pressured] == ["tests/test_a.py::test_slow[case-1]"]

    def test_a_doctest_key_is_taken_at_face_value(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"src/teatree/thing.py": "def f() -> None:\n    pass\n"},
            durations={"src/teatree/thing.py::teatree.thing.f": 58.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.pressured] == ["src/teatree/thing.py::teatree.thing.f"]

    def test_worst_offender_first(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _PLAIN, "tests/test_b.py": _PLAIN},
            durations={"tests/test_a.py::test_slow": 50.0, "tests/test_b.py::test_slow": 58.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.pressured] == [
            "tests/test_b.py::test_slow",
            "tests/test_a.py::test_slow",
        ]

    def test_no_pyproject_ceiling_is_unanswerable_not_healthy(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _PLAIN},
            durations={"tests/test_a.py::test_slow": 90.0},
            pyproject="[tool.pytest.ini_options]\n",
        )
        assert measure_timeout_headroom(repo) is None

    def test_unreadable_durations_file_raises_never_degrades_to_empty(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _PLAIN}, durations={})
        (repo / "dev" / ".test_durations").write_text("{not json", encoding="utf-8")
        with pytest.raises(DurationsUnreadableError):
            measure_timeout_headroom(repo)

    def test_nothing_recorded_reports_zero_judged(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _PLAIN}, durations={})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert (report.judged, report.pressured) == (0, ())

    def test_the_tight_band_is_the_one_the_ci_report_uses(self) -> None:
        assert 0.0 < TIGHT_FRACTION < 1.0


class TestAgainstThisRepo:
    """The guard must be non-vacuous on the real tree it ships in."""

    def test_measures_the_live_checkout_without_raising(self) -> None:
        report = measure_timeout_headroom(Path(__file__).resolve().parents[2])
        assert report is not None
        assert report.judged > 0
