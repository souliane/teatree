"""Declared-versus-INSTALLED version skew (#4049) — the drift the missing-deps check misses.

The host tool env sat three weeks behind ``pyproject.toml`` carrying ``mcp 1.28.1``
against a declared ``mcp>=2,<3``. Nothing was MISSING, so
:func:`teatree.utils.dep_drift.find_missing_dependencies` was silent, and the skew only
ever surfaced as an ``ImportError`` at the moment the MCP server had to start.
"""

from pathlib import Path

import pytest

from teatree.utils.dep_skew import find_version_skew

_PYPROJECT = """
[project]
name = "probe"
dependencies = [{deps}]
"""


@pytest.fixture
def pyproject(tmp_path: Path) -> Path:
    return tmp_path / "pyproject.toml"


def _write(path: Path, *deps: str) -> Path:
    path.write_text(_PYPROJECT.format(deps=", ".join(f'"{dep}"' for dep in deps)), encoding="utf-8")
    return path


class TestInstalledButTooOld:
    def test_a_dist_below_its_declared_floor_is_skew(self, pyproject: Path) -> None:
        """The exact fault: installed, importable, and useless."""
        skews = find_version_skew(_write(pyproject, "pytest>=9999"))

        assert [skew.name for skew in skews] == ["pytest"]
        assert skews[0].installed is not None
        assert ">=9999" in skews[0].summary

    def test_a_satisfied_declaration_is_not_skew(self, pyproject: Path) -> None:
        assert find_version_skew(_write(pyproject, "pytest>=1")) == []

    def test_an_unbounded_declaration_is_not_skew(self, pyproject: Path) -> None:
        assert find_version_skew(_write(pyproject, "pytest")) == []

    def test_a_dist_that_is_absent_entirely_is_reported_too(self, pyproject: Path) -> None:
        skews = find_version_skew(_write(pyproject, "definitely-not-installed-xyz>=1"))

        assert [(skew.name, skew.installed) for skew in skews] == [("definitely-not-installed-xyz", None)]
        assert "NOT INSTALLED" in skews[0].summary

    def test_the_real_pyproject_is_satisfied_by_this_environment(self) -> None:
        """The suite's own env must be current — otherwise every other test is suspect."""
        repo_pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"

        assert find_version_skew(repo_pyproject) == []

    def test_an_unparseable_requirement_is_skipped_rather_than_crashing(self, pyproject: Path) -> None:
        pyproject.write_text('[project]\ndependencies = ["=== nonsense ==="]\n', encoding="utf-8")

        assert find_version_skew(pyproject) == []


class TestAMarkerThatExcludesThisEnvironment:
    """A requirement this interpreter is not meant to satisfy is not skew — installed or not.

    The verdict feeds a self-repair, so a false skew reinstalls the operator's env to
    chase a dependency the marker says this platform never needed.
    """

    def test_an_excluded_dist_that_is_installed_out_of_range_is_not_skew(self, pyproject: Path) -> None:
        deps = "pytest>=9999; sys_platform == 'nonesuch'"

        assert find_version_skew(_write(pyproject, deps)) == []

    def test_an_excluded_dist_that_is_absent_is_not_skew(self, pyproject: Path) -> None:
        deps = "definitely-not-installed-xyz>=1; sys_platform == 'nonesuch'"

        assert find_version_skew(_write(pyproject, deps)) == []

    def test_a_marker_that_selects_this_environment_still_reports_skew(self, pyproject: Path) -> None:
        """Otherwise the fix would be indistinguishable from ignoring every marked dep."""
        deps = "pytest>=9999; python_version >= '3'"

        assert [skew.name for skew in find_version_skew(_write(pyproject, deps))] == ["pytest"]
