"""Unit tests for ``tests/_db_template.py``.

The cross-process template-DB sharing primitives that back the
``django_db_setup`` override in ``tests/conftest.py`` (perf(ci): skip
redundant per-xdist-worker migrate).
"""

import contextlib
import sqlite3
import threading
from pathlib import Path

from tests._db_template import build_or_reuse_template, restore_from_template


class _SimulatedBuildCrashError(RuntimeError):
    """Raised by a test double to simulate a build failing mid-way."""


class TestBuildOrReuseTemplate:
    def test_first_caller_builds_the_template(self, tmp_path: Path) -> None:
        template = tmp_path / "template.sqlite3"
        lock = tmp_path / "template.sqlite3.lock"
        calls: list[Path] = []

        def build(path: Path) -> None:
            calls.append(path)
            path.write_bytes(b"fake-db-bytes")

        build_or_reuse_template(template, lock, build)

        assert calls == [tmp_path / "template.sqlite3.building"]
        assert template.read_bytes() == b"fake-db-bytes"

    def test_second_caller_reuses_the_existing_template_without_rebuilding(self, tmp_path: Path) -> None:
        template = tmp_path / "template.sqlite3"
        lock = tmp_path / "template.sqlite3.lock"
        build_calls = 0

        def build(path: Path) -> None:
            nonlocal build_calls
            build_calls += 1
            path.write_bytes(b"first-build")

        build_or_reuse_template(template, lock, build)
        build_or_reuse_template(template, lock, build)

        assert build_calls == 1
        assert template.read_bytes() == b"first-build"

    def test_concurrent_callers_build_exactly_once(self, tmp_path: Path) -> None:
        template = tmp_path / "template.sqlite3"
        lock = tmp_path / "template.sqlite3.lock"
        build_calls = 0
        counter_guard = threading.Lock()

        def build(path: Path) -> None:
            nonlocal build_calls
            with counter_guard:
                build_calls += 1
            path.write_bytes(b"built")

        threads = [threading.Thread(target=build_or_reuse_template, args=(template, lock, build)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert build_calls == 1, "flock must serialize concurrent builders down to exactly one real build"

    def test_a_failed_build_never_leaves_a_partial_file_at_the_final_path(self, tmp_path: Path) -> None:
        template = tmp_path / "template.sqlite3"
        lock = tmp_path / "template.sqlite3.lock"

        def crashing_build(path: Path) -> None:
            path.write_bytes(b"partial")
            raise _SimulatedBuildCrashError

        with contextlib.suppress(_SimulatedBuildCrashError):
            build_or_reuse_template(template, lock, crashing_build)

        assert not template.exists(), "a failed build must not leave a file at the final template path"

    def test_a_retry_after_a_failed_build_succeeds(self, tmp_path: Path) -> None:
        template = tmp_path / "template.sqlite3"
        lock = tmp_path / "template.sqlite3.lock"

        def crashing_build(path: Path) -> None:
            path.write_bytes(b"partial")
            raise _SimulatedBuildCrashError

        def good_build(path: Path) -> None:
            path.write_bytes(b"good")

        with contextlib.suppress(_SimulatedBuildCrashError):
            build_or_reuse_template(template, lock, crashing_build)
        build_or_reuse_template(template, lock, good_build)

        assert template.read_bytes() == b"good"


class TestRestoreFromTemplate:
    def test_copies_schema_and_data_into_the_target_connection(self, tmp_path: Path) -> None:
        template = tmp_path / "template.sqlite3"
        source = sqlite3.connect(str(template))
        source.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        source.execute("INSERT INTO widgets (name) VALUES ('sprocket')")
        source.commit()
        source.close()

        target = sqlite3.connect(":memory:")
        try:
            restore_from_template(template, target)
            assert target.execute("SELECT name FROM widgets").fetchall() == [("sprocket",)]
        finally:
            target.close()

    def test_target_is_independent_of_the_template_after_restore(self, tmp_path: Path) -> None:
        template = tmp_path / "template.sqlite3"
        source = sqlite3.connect(str(template))
        source.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        source.commit()
        source.close()

        target_a = sqlite3.connect(":memory:")
        target_b = sqlite3.connect(":memory:")
        try:
            restore_from_template(template, target_a)
            restore_from_template(template, target_b)

            target_a.execute("INSERT INTO widgets (name) VALUES ('only-in-a')")
            target_a.commit()

            assert target_a.execute("SELECT name FROM widgets").fetchall() == [("only-in-a",)]
            assert target_b.execute("SELECT name FROM widgets").fetchall() == []
        finally:
            target_a.close()
            target_b.close()
