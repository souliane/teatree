"""Tests for ``tests/_db_template.py`` — the migrated-DB template restore helper (W7-PR2).

``schema_hash`` fingerprints exactly the inputs that shape a fresh ``migrate``
(sqlite version, every migration file's bytes, ``uv.lock``, ``tests/django_settings.py``)
so a template built from a stale schema is never silently reused.
``publish_from_connection``/``restore_into_connection`` round-trip a live sqlite3
connection through an on-disk template file via ``sqlite3.Connection.backup()`` —
the same primitive ``src/teatree/paths.py::_sqlite_snapshot`` uses for the
canonical-DB seed, here restoring FULL-OVERWRITE semantics into an in-memory
test DB (proving stale destination state cannot survive a restore).
"""

import sqlite3
from pathlib import Path

import pytest

from tests._db_template import (
    publish_from_connection,
    restore_into_connection,
    schema_hash,
    template_build_lock,
    template_path,
)


def _write_fake_repo(root: Path) -> None:
    migrations = root / "src" / "teatree" / "core" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "0001_initial.py").write_text("# migration\n")
    (root / "uv.lock").write_text("# lock\n")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "django_settings.py").write_text("# settings\n")


class TestSchemaHash:
    def test_stable_for_unchanged_inputs(self, tmp_path: Path) -> None:
        _write_fake_repo(tmp_path)
        assert schema_hash(tmp_path) == schema_hash(tmp_path)

    def test_changes_when_a_migration_file_byte_changes(self, tmp_path: Path) -> None:
        _write_fake_repo(tmp_path)
        before = schema_hash(tmp_path)
        migration = tmp_path / "src" / "teatree" / "core" / "migrations" / "0001_initial.py"
        migration.write_text(migration.read_text() + "\n# changed\n")
        assert schema_hash(tmp_path) != before

    def test_changes_when_a_new_migration_file_is_added(self, tmp_path: Path) -> None:
        _write_fake_repo(tmp_path)
        before = schema_hash(tmp_path)
        migrations = tmp_path / "src" / "teatree" / "core" / "migrations"
        (migrations / "0002_added.py").write_text("# new migration\n")
        assert schema_hash(tmp_path) != before

    def test_changes_when_uv_lock_changes(self, tmp_path: Path) -> None:
        _write_fake_repo(tmp_path)
        before = schema_hash(tmp_path)
        (tmp_path / "uv.lock").write_text("# lock changed\n")
        assert schema_hash(tmp_path) != before

    def test_changes_when_sqlite_version_changes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_fake_repo(tmp_path)
        before = schema_hash(tmp_path)
        monkeypatch.setattr(sqlite3, "sqlite_version", "0.0.0-test")
        assert schema_hash(tmp_path) != before

    def test_is_a_stable_short_hex_digest(self, tmp_path: Path) -> None:
        _write_fake_repo(tmp_path)
        digest = schema_hash(tmp_path)
        assert len(digest) == 16
        int(digest, 16)  # raises ValueError if not hex


class TestTemplatePath:
    def test_builds_a_digest_named_sqlite_file_under_the_template_dir(self, tmp_path: Path) -> None:
        path = template_path("deadbeef1234", template_dir=tmp_path)
        assert path == tmp_path / "template-deadbeef1234.sqlite3"


class TestPublishRestoreRoundTrip:
    def test_restore_fully_overwrites_the_destination(self, tmp_path: Path) -> None:
        source = sqlite3.connect(":memory:")
        try:
            source.execute("CREATE TABLE loops (name TEXT)")
            source.execute("INSERT INTO loops VALUES ('inbox')")
            source.commit()
            dest_template = tmp_path / "template-abc123.sqlite3"
            publish_from_connection(source, dest_template)
        finally:
            source.close()

        dest_conn = sqlite3.connect(":memory:")
        try:
            dest_conn.execute("CREATE TABLE junk (id INTEGER)")
            dest_conn.commit()

            restore_into_connection(dest_template, dest_conn)

            tables = {row[0] for row in dest_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert "junk" not in tables, "pre-existing destination tables must not survive a restore"
            assert "loops" in tables
            assert dest_conn.execute("SELECT name FROM loops").fetchall() == [("inbox",)]
        finally:
            dest_conn.close()


class TestPublishAtomicity:
    def test_no_tmp_file_left_behind_after_publish(self, tmp_path: Path) -> None:
        # A dedicated sub-directory (not tmp_path itself) — unrelated autouse
        # fixtures (e.g. _isolate_env) also write into tmp_path, which would
        # otherwise pollute the "no stray files" assertion below.
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        source = sqlite3.connect(":memory:")
        try:
            dest = template_dir / "template-abc123.sqlite3"
            publish_from_connection(source, dest)
        finally:
            source.close()
        leftovers = [p for p in template_dir.iterdir() if p != dest]
        assert leftovers == [], f"publish left stray files behind: {leftovers}"

    def test_prunes_stale_sibling_templates(self, tmp_path: Path) -> None:
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        stale = template_dir / "template-oldhash.sqlite3"
        stale.write_bytes(b"stale")
        source = sqlite3.connect(":memory:")
        try:
            dest = template_dir / "template-newhash.sqlite3"
            publish_from_connection(source, dest)
        finally:
            source.close()
        assert not stale.exists(), "a stale-hash template must be pruned on a fresh publish"
        assert dest.exists()

    def test_keeps_the_freshly_published_file(self, tmp_path: Path) -> None:
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        source = sqlite3.connect(":memory:")
        try:
            source.execute("CREATE TABLE t (x INTEGER)")
            source.commit()
            dest = template_dir / "template-freshhash.sqlite3"
            publish_from_connection(source, dest)
        finally:
            source.close()
        assert dest.exists()
        check = sqlite3.connect(dest)
        try:
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            check.close()
        assert "t" in tables


class TestTemplateBuildLock:
    def test_creates_the_lock_file_and_releases_on_exit(self, tmp_path: Path) -> None:
        with template_build_lock(tmp_path):
            pass
        assert (tmp_path / ".lock").exists()

    def test_can_be_reentered_sequentially_without_deadlock(self, tmp_path: Path) -> None:
        with template_build_lock(tmp_path):
            pass
        with template_build_lock(tmp_path):
            pass
