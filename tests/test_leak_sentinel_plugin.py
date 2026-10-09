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

    def test_all_env_keys_unions_added_removed_changed(self) -> None:
        before = Snapshot(env={"GONE": "1", "MUT": "2"}, cwd="/tmp")
        after = Snapshot(env={"NEW": "9", "MUT": "changed"}, cwd="/tmp")
        assert diff_snapshots(before, after).all_env_keys == frozenset({"NEW", "GONE", "MUT"})

    def test_ignore_cwd_suppresses_the_cwd_delta(self) -> None:
        diff = diff_snapshots(Snapshot(env={}, cwd="/a"), Snapshot(env={}, cwd="/b"), ignore_cwd=True)
        assert diff.cwd_from is None
        assert diff.is_empty


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


# A module-scoped fixture sets+restores an env var and cwd; the sentinel must NOT
# blame the first test for its setup nor the last for its teardown. A genuine per-test
# leak sits in the middle and MUST still be named (anti-vacuous both directions).
_SCOPED_FIXTURE_SUITE = """
import os
import pytest


@pytest.fixture(scope="module", autouse=True)
def _module_env(tmp_path_factory):
    origin = os.getcwd()
    target = tmp_path_factory.mktemp("modcwd")
    os.environ["_MODULE_SCOPED"] = "on"   # set ONCE at module setup (first test)...
    os.chdir(target)
    try:
        yield
    finally:
        os.chdir(origin)                  # ...restored ONCE at module teardown (last test)
        del os.environ["_MODULE_SCOPED"]


def test_first():
    assert os.environ["_MODULE_SCOPED"] == "on"


def test_middle_leaks():
    os.environ["_PER_TEST_LEAK"] = "dirty"   # genuine per-test leak -> POLLUTER


def test_last():
    assert os.environ["_MODULE_SCOPED"] == "on"
"""


# Only a well-behaved module-scoped fixture, no per-test leak: error mode must stay
# GREEN. Before the scope-aware fix this red'd the first + last test (env/cwd added on
# setup, removed on teardown), so a passing run here proves the false positive is gone.
_SCOPED_CLEAN_SUITE = """
import os
import pytest


@pytest.fixture(scope="module", autouse=True)
def _module_env():
    os.environ["_MODULE_SCOPED"] = "on"
    try:
        yield
    finally:
        del os.environ["_MODULE_SCOPED"]


def test_a():
    assert os.environ["_MODULE_SCOPED"] == "on"


def test_b():
    assert os.environ["_MODULE_SCOPED"] == "on"
"""


def _run_suite_source(tmp_path: Path, source: str, mode: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_toy_leaks.py").write_text(source, encoding="utf-8")
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
            "-p",
            "no:xdist",
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


def _run_toy_suite(tmp_path: Path, mode: str, *, xdist_workers: int = 0) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_toy_leaks.py").write_text(_LEAKY_SUITE, encoding="utf-8")
    env = dict(_clean_env())
    env["PYTHONPATH"] = str(_REPO_ROOT)
    xdist_args = ["-n", str(xdist_workers), "--dist", "load"] if xdist_workers else ["-p", "no:xdist"]
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
            *xdist_args,
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


@pytest.mark.integration
class TestSentinelUnderXdist:
    """Warn-mode findings must fan IN to the controller under xdist (CI-1).

    Under xdist the leaks are detected on WORKERS, whose ``pytest_terminal_summary``
    never fires (no terminal), so before the fix warn-mode named no polluter under
    the exact sharded lane it instruments — a silent no-op. The worker→controller
    ``workeroutput`` fan-in restores it.
    """

    def test_warn_mode_names_polluters_under_xdist(self, tmp_path: Path) -> None:
        result = _run_toy_suite(tmp_path, "warn", xdist_workers=2)
        combined = result.stdout + result.stderr
        # warn mode still never reds the run, even under xdist.
        assert result.returncode == 0, combined
        # the summary printed on the CONTROLLER must name each worker-detected polluter.
        assert "POLLUTER test_toy_leaks.py::test_env_polluter" in combined, combined
        assert "_SENTINEL_LEAK" in combined, combined
        assert "POLLUTER test_toy_leaks.py::test_cwd_polluter" in combined, combined

    def test_error_mode_still_fails_the_polluter_under_xdist(self, tmp_path: Path) -> None:
        result = _run_toy_suite(tmp_path, "error", xdist_workers=2)
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "leaked process-global state" in combined, combined


@pytest.mark.integration
class TestSentinelIgnoresScopedFixtures:
    """A module/session-scoped fixture that sets+restores state is NOT a polluter (CI-8).

    The per-test snapshot boundary is asymmetric for a scoped fixture: it sets up during
    the first test (after that test's baseline) and tears down during the last (before
    that test's final), so a naive diff blames the first test for the ``env added`` and
    the last for the ``env removed`` the fixture legitimately owns. The scope-aware
    tracking excludes those, while a genuine per-test leak is still named.
    """

    def test_scoped_fixture_not_flagged_but_per_test_leak_is(self, tmp_path: Path) -> None:
        result = _run_suite_source(tmp_path, _SCOPED_FIXTURE_SUITE, "warn")
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        # The module-scoped fixture's env var + cwd are its own — never a polluter.
        assert "_MODULE_SCOPED" not in combined, combined
        assert "test_first" not in combined.split("leak sentinel")[-1], combined
        assert "test_last" not in combined.split("leak sentinel")[-1], combined
        # ...but the genuine per-test leak in the middle IS named.
        assert "POLLUTER test_toy_leaks.py::test_middle_leaks" in combined, combined
        assert "_PER_TEST_LEAK" in combined, combined

    def test_clean_scoped_fixture_suite_passes_error_mode(self, tmp_path: Path) -> None:
        # Anti-vacuous the other direction: with only a well-behaved module fixture and
        # NO per-test leak, error mode stays green. On the pre-fix code the first + last
        # test errored on the scoped setup/teardown, so this run would have been RED.
        result = _run_suite_source(tmp_path, _SCOPED_CLEAN_SUITE, "error")
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "leaked process-global state" not in combined, combined
        assert "_MODULE_SCOPED" not in combined, combined


def _clean_env() -> dict[str, str]:
    import os  # noqa: PLC0415 — local so the module stays a thin plugin test

    # Strip GIT_* so an inline pre-commit ``pytest`` run's exported git env can't leak
    # into the toy subprocess (AGENTS.md § Test-Writing Doctrine).
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
