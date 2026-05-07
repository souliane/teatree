"""Tests for ``teatree.utils.dep_drift`` — editable-install drift detection.

The module powers ``t3 setup``'s self-healing path: when teatree's
``pyproject.toml`` adds a new dep, every existing editable install hits
``ModuleNotFoundError`` until the user re-runs
``uv tool install --editable . --reinstall``.  ``dep_drift`` detects the
condition; the wiring in ``cli/setup.py`` repairs it automatically.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.utils.dep_drift import (
    declared_dependency_names,
    editable_source_path,
    find_missing_dependencies,
    normalize,
)


class TestNormalize:
    """PEP 503 distribution-name normalization."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Django", "django"),
            ("django_typer", "django-typer"),
            ("django.typer", "django-typer"),
            ("DJANGO__TYPER", "django-typer"),
            ("django-tasks-db", "django-tasks-db"),
            ("PyYAML", "pyyaml"),
        ],
    )
    def test_normalises_to_pep503(self, raw: str, expected: str) -> None:
        assert normalize(raw) == expected


class TestDeclaredDependencyNames:
    """Parsing ``[project].dependencies`` from ``pyproject.toml``."""

    def test_extracts_plain_names(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            'dependencies = ["django>=5.2,<6.1", "httpx>=0.27", "tomlkit>=0.13"]\n',
            encoding="utf-8",
        )
        assert declared_dependency_names(pyproject) == {"django", "httpx", "tomlkit"}

    def test_normalises_names(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["Django_Typer>=3", "Django.Tasks.DB"]\n',
            encoding="utf-8",
        )
        assert declared_dependency_names(pyproject) == {"django-typer", "django-tasks-db"}

    def test_strips_extras_and_markers(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\n'
            'dependencies = ["foo[extra]>=1", "bar; python_version < \'4\'", "baz==1.0"]\n',
            encoding="utf-8",
        )
        assert declared_dependency_names(pyproject) == {"foo", "bar", "baz"}

    def test_empty_when_section_missing(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
        assert declared_dependency_names(pyproject) == set()


class TestFindMissingDependencies:
    """Drift detection: declared but not installed."""

    def test_returns_missing_subset(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["django", "httpx", "tomlkit"]\n',
            encoding="utf-8",
        )
        with patch(
            "teatree.utils.dep_drift.installed_distribution_names",
            return_value={"django", "httpx"},
        ):
            assert find_missing_dependencies(pyproject) == ["tomlkit"]

    def test_empty_when_in_sync(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\nversion = "0"\ndependencies = ["django", "httpx"]\n',
            encoding="utf-8",
        )
        with patch(
            "teatree.utils.dep_drift.installed_distribution_names",
            return_value={"django", "httpx", "extra"},
        ):
            assert find_missing_dependencies(pyproject) == []


class TestEditableSourcePath:
    """Detecting the editable source via PEP 660 ``direct_url.json``."""

    def test_returns_path_when_editable(self, tmp_path: Path) -> None:
        direct_url = json.dumps(
            {"url": f"file://{tmp_path}/clone", "dir_info": {"editable": True}},
        )
        fake_dist = _FakeDist({"direct_url.json": direct_url})
        with patch("teatree.utils.dep_drift.distribution", return_value=fake_dist):
            assert editable_source_path() == Path(f"{tmp_path}/clone")

    def test_returns_none_when_not_editable(self, tmp_path: Path) -> None:
        direct_url = json.dumps(
            {"url": f"file://{tmp_path}/clone", "dir_info": {"editable": False}},
        )
        fake_dist = _FakeDist({"direct_url.json": direct_url})
        with patch("teatree.utils.dep_drift.distribution", return_value=fake_dist):
            assert editable_source_path() is None

    def test_returns_none_when_direct_url_absent(self) -> None:
        fake_dist = _FakeDist({})
        with patch("teatree.utils.dep_drift.distribution", return_value=fake_dist):
            assert editable_source_path() is None

    def test_returns_none_when_direct_url_unparsable(self) -> None:
        fake_dist = _FakeDist({"direct_url.json": "not json {{{"})
        with patch("teatree.utils.dep_drift.distribution", return_value=fake_dist):
            assert editable_source_path() is None

    def test_returns_none_when_url_not_file_scheme(self) -> None:
        direct_url = json.dumps(
            {"url": "https://example.com/wheel", "dir_info": {"editable": True}},
        )
        fake_dist = _FakeDist({"direct_url.json": direct_url})
        with patch("teatree.utils.dep_drift.distribution", return_value=fake_dist):
            assert editable_source_path() is None


class _FakeDist:
    """Stand-in for ``importlib.metadata.Distribution`` exposing ``read_text``."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def read_text(self, name: str) -> str | None:
        return self._files.get(name)
