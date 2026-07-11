"""The leak-sentinel plugin: name the POLLUTER that leaks env/cwd, not its victim.

Unit arm: the pure snapshot/diff logic. Integration arm: a real ``pytest`` subprocess
over a toy suite where one test leaks an env var and one leaks cwd — the plugin must
name the polluter in ``warn`` mode (without failing the run) and error the polluter in
``error`` mode, while a well-behaved suite stays clean.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.leak_sentinel_plugin import LeakDiff, Snapshot, diff_snapshots

_REPO_ROOT = Path(__file__).resolve().parents[1]


class TestDiffSnapshots:
    def test_clean_when_nothing_changed(self) -> None:
        snap = Snapshot(env={"A": "1", "B": "2"}, cwd="/tmp")
        assert diff_snapshots(snap, snap).is_empty

    def test_flags_added_and_removed_and_changed_env_keys(self) -> None:
        before = Snapshot(env={"KEEP": "1", "GONE": "2", "MUT": "3"}, cwd="/tmp")
        after = Snapshot(env={"KEEP": "1", "NEW": "9", "MUT": "changed"}, cwd="/tmp")
        diff = diff_snapshots(before, after)
        assert diff.env_added == ("NEW",)
        assert diff.env_removed == ("GONE",)
        assert diff.env_changed == ("MUT",)
        assert not diff.is_empty
        assert "env added ['NEW']" in diff.describe()

    def test_flags_cwd_change(self) -> None:
        diff = diff_snapshots(Snapshot(env={}, cwd="/a"), Snapshot(env={}, cwd="/b"))
        assert diff.cwd_from == "/a"
        assert diff.cwd_to == "/b"
        assert "cwd '/a' -> '/b'" in diff.describe()

    def test_volatile_keys_are_ignored(self) -> None:
        before = Snapshot(env={"PYTEST_CURRENT_TEST": "one"}, cwd="/tmp")
        after = Snapshot(env={"PYTEST_CURRENT_TEST": "two"}, cwd="/tmp")
        assert diff_snapshots(before, after).is_empty

    def test_capture_reads_live_process_surface(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("_LEAK_SENTINEL_PROBE", "present")
        snap = Snapshot.capture()
        assert snap.env["_LEAK_SENTINEL_PROBE"] == "present"
        assert snap.cwd == str(Path.cwd())

    def test_empty_diff_describes_to_empty_string(self) -> None:
        assert LeakDiff((), (), (), None, None).describe() == ""


_LEAKY_SUITE = """
import os


def test_env_polluter():
    os.environ["_SENTINEL_LEAK"] = "dirty"  # never restored -> polluter


def test_cwd_polluter(tmp_path):
    os.chdir(tmp_path)  # never restored -> polluter


def test_clean(monkeypatch, tmp_path):
    monkeypatch.setenv("_SENTINEL_OK", "reverts")  # monkeypatch reverts -> not a leak
    monkeypatch.chdir(tmp_path)                     # monkeypatch reverts -> not a leak
    assert True
"""


def _run_toy_suite(tmp_path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_toy_leaks.py").write_text(_LEAKY_SUITE, encoding="utf-8")
    env = dict(_clean_env())
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_toy_leaks.py",
            "-p",
            "scripts.ci.leak_sentinel_plugin",
            "-p",
            "no:randomly",
            f"--leak-sentinel={mode}",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-q",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.integration
class TestSentinelNamesThePolluter:
    def test_warn_mode_names_polluters_without_failing_the_run(self, tmp_path: Path) -> None:
        result = _run_toy_suite(tmp_path, "warn")
        combined = result.stdout + result.stderr
        # warn mode never reds the run: all three toy tests still pass.
        assert result.returncode == 0, combined
        assert "POLLUTER test_toy_leaks.py::test_env_polluter" in combined, combined
        assert "_SENTINEL_LEAK" in combined
        assert "POLLUTER test_toy_leaks.py::test_cwd_polluter" in combined, combined
        # the monkeypatch-clean test is never flagged.
        assert "test_clean" not in combined.split("leak sentinel")[-1]

    def test_error_mode_fails_the_polluter(self, tmp_path: Path) -> None:
        result = _run_toy_suite(tmp_path, "error")
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "leaked process-global state" in combined, combined
        assert "test_env_polluter" in combined

    def test_off_mode_is_silent(self, tmp_path: Path) -> None:
        result = _run_toy_suite(tmp_path, "off")
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "POLLUTER" not in combined
        assert "leak sentinel" not in combined


def _clean_env() -> dict[str, str]:
    import os  # noqa: PLC0415 — local so the module stays a thin plugin test

    # Strip GIT_* so an inline pre-commit ``pytest`` run's exported git env can't leak
    # into the toy subprocess (AGENTS.md § Test-Writing Doctrine).
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
