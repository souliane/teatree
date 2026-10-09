"""Shared fixtures for the django_db test package.

The autouse ``_isolate_bad_artifacts`` fixture is lifted verbatim from the
former monolithic ``tests/test_django_db.py`` (souliane/teatree#443). It was
declared module-level autouse there, so it must live in ``conftest.py`` to
keep applying to every focused test module after the split. No behavior
change.
"""

from pathlib import Path

import pytest

from teatree.utils import bad_artifacts


@pytest.fixture(autouse=True)
def _isolate_bad_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bad_artifacts, "_CACHE_FILE", tmp_path / "bad_artifacts.json")
