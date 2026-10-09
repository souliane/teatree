"""Module-level fixtures for the teatree config test package.

Every setting is DB-home now; ``config_db`` points the Django-free ``cold_reader``
at a per-test sqlite (via ``T3_CONFIG_DB``) so a test can seed the ``overlays`` /
``e2e_repos`` registries and other cold-read keys without a config file.
"""

from pathlib import Path

import pytest


@pytest.fixture
def config_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A per-test config-store sqlite path wired to the cold reader via ``T3_CONFIG_DB``."""
    db = tmp_path / "config.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(db))
    return db


@pytest.fixture
def no_installed_overlays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``importlib.metadata.entry_points`` report no teatree overlays.

    Installed teatree entry points (``t3-teatree``) would otherwise leak
    into overlay discovery and shadow the seeded fixtures.
    """
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the test from a cwd free of ``manage.py`` ancestors."""
    away = tmp_path / "no_manage"
    away.mkdir()
    monkeypatch.chdir(away)
    return away
