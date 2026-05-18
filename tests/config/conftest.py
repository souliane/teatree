"""Module-level fixtures for the teatree config test package.

Lifted verbatim from the former monolithic ``tests/test_config.py``
(souliane/teatree#443). These three fixtures were shared across the
overlay-discovery, mode, override, check-for-updates and dirs concerns;
hoisting them here keeps them available to every split module without
duplication. No behavior change.
"""

from pathlib import Path

import pytest


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage a real ``~/.teatree.toml`` under ``tmp_path`` and wire it to the module."""
    cfg = tmp_path / ".teatree.toml"
    monkeypatch.setattr("teatree.config.CONFIG_PATH", cfg)
    return cfg


@pytest.fixture
def no_installed_overlays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``importlib.metadata.entry_points`` report no teatree overlays.

    Installed teatree entry points (``t3-teatree``) would otherwise leak
    into overlay discovery and shadow the TOML fixtures.
    """
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **_kw: [])


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the test from a cwd free of ``manage.py`` ancestors."""
    away = tmp_path / "no_manage"
    away.mkdir()
    monkeypatch.chdir(away)
    return away
