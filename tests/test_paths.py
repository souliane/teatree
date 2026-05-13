"""Tests for ``teatree.paths`` helpers."""

from pathlib import Path

from teatree.paths import find_stale_dbs


def test_no_stale_dbs(tmp_path: Path) -> None:
    canonical = tmp_path / "db.sqlite3"
    canonical.touch()
    assert list(find_stale_dbs(tmp_path, canonical=canonical)) == []


def test_skips_missing_data_dir(tmp_path: Path) -> None:
    missing = tmp_path / "absent"
    assert list(find_stale_dbs(missing, canonical=missing / "db.sqlite3")) == []


def test_finds_legacy_namespaced_layout(tmp_path: Path) -> None:
    canonical = tmp_path / "db.sqlite3"
    canonical.touch()
    stale_a = tmp_path / "teatree" / "db.sqlite3"
    stale_b = tmp_path / "dev" / "db.sqlite3"
    stale_a.parent.mkdir()
    stale_b.parent.mkdir()
    stale_a.touch()
    stale_b.touch()

    found = sorted(find_stale_dbs(tmp_path, canonical=canonical))
    assert found == sorted([stale_a, stale_b])


def test_finds_nested_layouts(tmp_path: Path) -> None:
    canonical = tmp_path / "db.sqlite3"
    canonical.touch()
    nested = tmp_path / "a" / "b" / "c" / "db.sqlite3"
    nested.parent.mkdir(parents=True)
    nested.touch()

    assert list(find_stale_dbs(tmp_path, canonical=canonical)) == [nested]
