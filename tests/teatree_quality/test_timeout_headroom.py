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

_MARKED_AT_LANE = """
import pytest


@pytest.mark.timeout(60)
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

_MARKED_AND_UNMARKED = """
import pytest


@pytest.mark.timeout(240)
def test_marked() -> None:
    pass


def test_unmarked() -> None:
    pass
"""

_MARKED_CLASS_AND_LOOSE_TEST = """
import pytest


@pytest.mark.timeout(240)
class TestSlowGroup:
    def test_inside(self) -> None:
        pass


def test_outside() -> None:
    pass
"""

_MODULE_MARKED = """
import pytest

pytestmark = pytest.mark.timeout(240)


def test_slow() -> None:
    pass
"""

_CLASS_BODY_MARKED = """
import pytest


class TestSlowGroup:
    pytestmark = pytest.mark.timeout(240)

    def test_inside(self) -> None:
        pass


def test_outside() -> None:
    pass
"""

_ALIASED_MARKER = """
import pytest

SLOW = pytest.mark.timeout(240)


@SLOW
def test_slow() -> None:
    pass
"""

_STACKED_DECORATORS = """
import pytest


@pytest.mark.timeout(300)
@pytest.mark.timeout(40)
def test_slow() -> None:
    pass
"""

_CLASS_DECORATOR_OVER_BODY = """
import pytest


@pytest.mark.timeout(300)
class TestSlowGroup:
    pytestmark = pytest.mark.timeout(40)

    def test_inside(self) -> None:
        pass
"""

_MODULE_MARK_LIST = """
import pytest

pytestmark = [pytest.mark.timeout(50), pytest.mark.timeout(300)]


def test_slow() -> None:
    pass
"""

_CLASS_BODY_MARK_LIST = """
import pytest


class TestSlowGroup:
    pytestmark = [pytest.mark.timeout(50), pytest.mark.timeout(300)]

    def test_inside(self) -> None:
        pass
"""

_NAMED_CEILING_AND_UNMARKED = """
import pytest

BUDGET = 240


@pytest.mark.timeout(BUDGET)
def test_named() -> None:
    pass


def test_unmarked() -> None:
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


class TestMarkerResolutionIsPerTest:
    """``pytest_timeout`` resolves the ceiling per ITEM, so the judge must too.

    A file-wide ceiling lets one test's marker cover every other test in the same
    file — a real over-run of the applicable lane ceiling reported as healthy,
    which is the silent-green class this epic exists to remove.
    """

    def test_a_marker_on_one_test_leaves_its_neighbour_on_the_lane_ceiling(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _MARKED_AND_UNMARKED},
            durations={"tests/test_a.py::test_marked": 100.0, "tests/test_a.py::test_unmarked": 75.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [(pressure.node_id, pressure.ceiling) for pressure in report.over_ceiling] == [
            ("tests/test_a.py::test_unmarked", 60.0)
        ]
        assert not report.is_healthy

    def test_a_class_marker_covers_its_methods_and_nothing_else(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _MARKED_CLASS_AND_LOOSE_TEST},
            durations={
                "tests/test_a.py::TestSlowGroup::test_inside": 100.0,
                "tests/test_a.py::test_outside": 75.0,
            },
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [(pressure.node_id, pressure.ceiling) for pressure in report.over_ceiling] == [
            ("tests/test_a.py::test_outside", 60.0)
        ]

    def test_a_class_body_pytestmark_covers_its_methods_and_nothing_else(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _CLASS_BODY_MARKED},
            durations={
                "tests/test_a.py::TestSlowGroup::test_inside": 100.0,
                "tests/test_a.py::test_outside": 75.0,
            },
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [(pressure.node_id, pressure.ceiling) for pressure in report.over_ceiling] == [
            ("tests/test_a.py::test_outside", 60.0)
        ]

    def test_a_module_pytestmark_covers_every_test_in_the_file(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _MODULE_MARKED},
            durations={"tests/test_a.py::test_slow": 100.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.pressured == ()

    def test_a_marker_bound_to_a_module_name_still_states_its_ceiling(self, tmp_path: Path) -> None:
        """``_SCAN_TIMEOUT = pytest.mark.timeout(300)`` then ``@_SCAN_TIMEOUT`` — the live tree's shape."""
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _ALIASED_MARKER},
            durations={"tests/test_a.py::test_slow": 100.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.pressured == ()

    def test_a_named_ceiling_silences_only_the_test_it_applies_to(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _NAMED_CEILING_AND_UNMARKED},
            durations={"tests/test_a.py::test_named": 100.0, "tests/test_a.py::test_unmarked": 75.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.over_ceiling] == ["tests/test_a.py::test_unmarked"]
        assert report.unresolved_ceilings == 1


class TestTheFirstStoredMarkWins:
    """Ties resolve to the mark pytest STORES FIRST, measured against real collection.

    Decorators apply bottom-up and a class body runs before its decorators, so the
    bottom-most decorator and the class-body ``pytestmark`` are the ones stored first
    — and ``get_closest_marker`` returns those, not the ones read first in source
    order. Guessing source order over-states the ceiling, which is the silent-green
    direction: a test pytest caps at 40s judged against a believed 300s reads healthy
    while it over-runs.
    """

    def test_the_bottom_most_of_stacked_decorators_applies(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _STACKED_DECORATORS},
            durations={"tests/test_a.py::test_slow": 45.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [(pressure.node_id, pressure.ceiling) for pressure in report.over_ceiling] == [
            ("tests/test_a.py::test_slow", 40.0)
        ]

    def test_a_class_body_pytestmark_beats_a_decorator_on_the_same_class(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _CLASS_DECORATOR_OVER_BODY},
            durations={"tests/test_a.py::TestSlowGroup::test_inside": 45.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [(pressure.node_id, pressure.ceiling) for pressure in report.over_ceiling] == [
            ("tests/test_a.py::TestSlowGroup::test_inside", 40.0)
        ]

    def test_the_first_element_of_a_module_pytestmark_list_applies(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _MODULE_MARK_LIST},
            durations={"tests/test_a.py::test_slow": 55.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [(pressure.node_id, pressure.ceiling) for pressure in report.over_ceiling] == [
            ("tests/test_a.py::test_slow", 50.0)
        ]

    def test_the_first_element_of_a_class_pytestmark_list_applies(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _CLASS_BODY_MARK_LIST},
            durations={"tests/test_a.py::TestSlowGroup::test_inside": 55.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [(pressure.node_id, pressure.ceiling) for pressure in report.over_ceiling] == [
            ("tests/test_a.py::TestSlowGroup::test_inside", 50.0)
        ]


class TestShieldedByItsOwnCeiling:
    """`shielded` is what the report knows that `pressured` cannot say: the marker path is working."""

    def test_a_marked_test_past_the_lane_band_is_shielded_not_pressured(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _MARKED}, durations={"tests/test_a.py::test_slow": 100.0})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [(pressure.node_id, pressure.ceiling) for pressure in report.shielded] == [
            ("tests/test_a.py::test_slow", 240.0)
        ]
        assert report.pressured == ()

    def test_an_unmarked_test_past_the_band_is_pressured_and_shields_nothing(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _PLAIN}, durations={"tests/test_a.py::test_slow": 56.85})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.pressured] == ["tests/test_a.py::test_slow"]
        assert report.shielded == ()

    def test_a_marker_restating_the_lane_value_shields_nothing(self, tmp_path: Path) -> None:
        """A ceiling that raises nothing is not protection — it is the lane value written twice."""
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _MARKED_AT_LANE},
            durations={"tests/test_a.py::test_slow": 56.85},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.pressured] == ["tests/test_a.py::test_slow"]
        assert report.shielded == ()

    def test_a_raised_ceiling_the_test_is_still_eating_is_not_shielding_it(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _MARKED}, durations={"tests/test_a.py::test_slow": 240.2})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.over_ceiling] == ["tests/test_a.py::test_slow"]
        assert report.shielded == ()

    def test_a_stale_key_shields_nothing_so_it_cannot_fake_a_live_marker(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _MARKED},
            durations={"tests/test_a.py::test_under_its_old_name": 100.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.shielded == ()
        assert report.pressured == ()

    def test_a_marked_test_below_the_band_is_never_a_candidate(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, sources={"tests/test_a.py": _MARKED}, durations={"tests/test_a.py::test_slow": 1.0})
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.shielded == ()
        assert report.pressured == ()

    def test_a_ceiling_named_rather_than_written_shields_nothing(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _NAMED_CEILING},
            durations={"tests/test_a.py::test_slow": 100.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert report.shielded == ()
        assert report.unresolved_ceilings == 1

    def test_the_worst_squeeze_is_named_first(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            sources={"tests/test_a.py": _MARKED, "tests/test_b.py": _MARKED},
            durations={"tests/test_a.py::test_slow": 100.0, "tests/test_b.py::test_slow": 170.0},
        )
        report = measure_timeout_headroom(repo)
        assert report is not None
        assert [pressure.node_id for pressure in report.shielded] == [
            "tests/test_b.py::test_slow",
            "tests/test_a.py::test_slow",
        ]


class TestAgainstThisRepo:
    """The guard must be non-vacuous on the real tree it ships in."""

    def test_measures_the_live_checkout_without_raising(self) -> None:
        report = measure_timeout_headroom(Path(__file__).resolve().parents[2])
        assert report is not None
        assert report.judged > 0

    def test_a_recorded_test_is_kept_off_the_report_by_its_own_stated_ceiling(self) -> None:
        """Derived from the cassette, never pinned in source (#4664 item 4, #4670).

        ``refresh-durations`` stages only ``dev/.test_durations``, so a node list
        hardcoded here desynchronises on every refresh — twice now, the second time
        reddening six CI jobs on a PR that changed no source at all. The cassette owns
        which tests are slow; all this asserts is that the marker path is live on
        whatever the cassette currently says, which is the one claim a refresh cannot
        invalidate while any marked slow test survives.
        """
        report = measure_timeout_headroom(Path(__file__).resolve().parents[2])
        assert report is not None
        assert report.shielded, (
            "no recorded test runs past the tight band while its own `@pytest.mark.timeout` "
            "keeps it off the report, so nothing here exercises the marker path (#4369)."
        )
