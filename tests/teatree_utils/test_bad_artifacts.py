"""Tests for teatree.utils.bad_artifacts — bad artifact cache."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from teatree.utils import bad_artifacts as mod


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_CACHE_FILE", tmp_path / "bad_artifacts.json")


class TestMarkAndCheck:
    def test_mark_bad_and_check(self) -> None:
        assert mod.is_bad("/tmp/dump.pgsql") is False
        mod.mark_bad("/tmp/dump.pgsql")
        assert mod.is_bad("/tmp/dump.pgsql") is True

    def test_mark_idempotent(self) -> None:
        mod.mark_bad("/tmp/a.pgsql")
        mod.mark_bad("/tmp/a.pgsql")
        assert mod.list_bad().count("/tmp/a.pgsql") == 1

    def test_unmark(self) -> None:
        mod.mark_bad("/tmp/b.pgsql")
        mod.unmark("/tmp/b.pgsql")
        assert mod.is_bad("/tmp/b.pgsql") is False

    def test_unmark_nonexistent(self) -> None:
        mod.unmark("/tmp/nope.pgsql")  # no error

    def test_dslr_key(self) -> None:
        mod.mark_bad("dslr:20260326_development-acme")
        assert mod.is_bad("dslr:20260326_development-acme") is True
        assert mod.is_bad("dslr:20260320_development-acme") is False


class TestListAndClear:
    def test_list_empty(self) -> None:
        assert mod.list_bad() == []

    def test_list_returns_all(self) -> None:
        mod.mark_bad("/tmp/a.pgsql")
        mod.mark_bad("dslr:snap1")
        assert len(mod.list_bad()) == 2

    def test_clear_all(self) -> None:
        mod.mark_bad("/tmp/a.pgsql")
        mod.clear_all()
        assert mod.list_bad() == []

    def test_clear_when_no_file(self) -> None:
        mod.clear_all()  # no error


class TestConcurrentMarks:
    def test_parallel_marks_all_survive(self) -> None:
        # Two importers marking different dumps at once: an unlocked read-modify-write
        # lets the later writer publish a list read before the earlier one landed.
        paths = [f"/tmp/dump-{n}.pgsql" for n in range(24)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(mod.mark_bad, paths))
        assert sorted(mod.list_bad()) == sorted(paths)

    def test_an_interrupted_publish_leaves_the_previous_ledger_readable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A truncating in-place write that dies mid-way loses every earlier mark.
        mod.mark_bad("/tmp/first.pgsql")

        def truncate_then_die(target: Path, *_args: object, **_kwargs: object) -> int:
            target.write_bytes(b"")
            msg = "disk full"
            raise OSError(msg)

        monkeypatch.setattr(Path, "write_text", truncate_then_die)
        with contextlib.suppress(OSError):
            mod.mark_bad("/tmp/second.pgsql")
        assert "/tmp/first.pgsql" in mod.list_bad()


class TestCorruptCache:
    def test_handles_corrupt_json(self, tmp_path: Path) -> None:
        (tmp_path / "bad_artifacts.json").write_text("not json", encoding="utf-8")
        assert mod.list_bad() == []

    def test_handles_non_list_json(self, tmp_path: Path) -> None:
        (tmp_path / "bad_artifacts.json").write_text('{"key": "value"}', encoding="utf-8")
        assert mod.list_bad() == []
