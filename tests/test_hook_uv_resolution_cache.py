"""``resolve_uv`` must be nearly free on the hook hot path — and still never lie.

The banned-terms gate runs on every commit and the leak gate on every push, so
resolution happens constantly. Two things used to make it expensive and wrong at
the same time: a version-manager shim was ACCEPTED as the answer whenever the
pinned interpreter happened to be installed (so the slow indirection was handed
back to the caller to ``exec``, and the file never delivered its own headline
contract), and ``uv --version`` was spawned afresh on every hook run.

These tests drive the real ``resolve-uv.sh`` from bash against planted
candidates, with every probe root redirected, so the runs are hermetic against
whatever uv the developer machine really has. They pin four properties:

+ a ``#!`` wrapper loses to a native binary, and is never even executed;
+ the answer is cached on disk, and a cache hit spawns nothing;
+ the cache is invalidated when the candidate SET changes, so a newly installed
    or removed uv is picked up without anyone clearing anything;
+ ``T3_UV`` bypasses the cache entirely, so the operator override can never be
    masked by a stale entry.

The counting is done by having each planted candidate append a line to a probe
log when it is RUN, which makes "spawned no subprocess" an observable fact
rather than a timing guess.
"""

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIB = _REPO_ROOT / "scripts" / "hooks" / "lib" / "resolve-uv.sh"

# Absolute, so the run is not at the mercy of the harness's own redirected PATH —
# and resolved from PATH rather than hardcoded, because that is the bash the
# hooks themselves are executed by.
_BASH = shutil.which("bash") or "/bin/bash"

# A version-manager shim: a ``#!`` script. It answers ``--version`` fine here —
# the point is that a native binary must still win, and that the shim is skipped
# without being run at all.
_SHIM_BODY = """#!/usr/bin/env bash
echo "$0" >> "${PROBE_LOG}"
if [ "${1:-}" = "--version" ]; then echo "uv 0.0.0-shim"; exit 0; fi
exit 0
"""

# A stand-in for the real, compiled uv: no shebang, so its first two bytes are
# not ``#!`` and ``_uv_is_native`` classifies it as native. bash still runs it
# (execve reports ENOEXEC and bash falls back to sh), which is all the resolver's
# behavioural check needs.
_NATIVE_BODY = """echo "$0" >> "${PROBE_LOG}"
if [ "${1:-}" = "--version" ]; then echo "uv 0.0.0-native"; exit 0; fi
exit 0
"""


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _Harness:
    """A hermetic home + cache + PATH in which ``resolve_uv`` can be sourced and run."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.cache = tmp_path / "cache"
        self.pathdir = tmp_path / "pathbin"
        self.probe_log = tmp_path / "probe.log"
        for d in (self.home, self.cache, self.pathdir):
            d.mkdir(parents=True, exist_ok=True)
        self.probe_log.write_text("", encoding="utf-8")

    @property
    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.cache),
            "PYENV_ROOT": str(self.home / ".pyenv"),
            "ASDF_DATA_DIR": str(self.home / ".asdf"),
            "PATH": f"{self.pathdir}:/usr/bin:/bin",
            "PROBE_LOG": str(self.probe_log),
        }

    def plant_path_shim(self) -> Path:
        return _executable(self.pathdir / "uv", _SHIM_BODY)

    def plant_native(self, version: str = "3.13.1") -> Path:
        return _executable(self.home / ".pyenv" / "versions" / version / "bin" / "uv", _NATIVE_BODY)

    def plant_installer_target(self) -> Path:
        return _executable(self.home / ".local" / "bin" / "uv", _NATIVE_BODY)

    def resolve(self, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        script = f'set -u; . "{_LIB}"; resolve_uv'
        return subprocess.run(
            [_BASH, "-c", script],
            capture_output=True,
            text=True,
            env={**self.env, **(extra_env or {})},
            check=False,
        )

    @property
    def probes(self) -> list[str]:
        return [line for line in self.probe_log.read_text(encoding="utf-8").splitlines() if line]

    def clear_probes(self) -> None:
        self.probe_log.write_text("", encoding="utf-8")

    @property
    def cache_file(self) -> Path:
        return self.cache / "teatree" / "uv-resolved"


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    return _Harness(tmp_path)


class TestNativePreferredOverWrapper:
    def test_native_binary_wins_over_a_runnable_path_shim(self, harness: _Harness) -> None:
        harness.plant_path_shim()
        native = harness.plant_native()

        result = harness.resolve()

        assert result.returncode == 0
        assert result.stdout == str(native)

    def test_the_shim_is_never_executed_when_a_native_binary_exists(self, harness: _Harness) -> None:
        shim = harness.plant_path_shim()
        harness.plant_native()

        harness.resolve()

        assert str(shim) not in harness.probes

    def test_a_wrapper_only_host_still_resolves(self, harness: _Harness) -> None:
        shim = harness.plant_path_shim()

        result = harness.resolve()

        assert result.returncode == 0
        assert result.stdout == str(shim)

    def test_no_candidate_at_all_fails_closed(self, harness: _Harness) -> None:
        assert harness.resolve().returncode == 1

    def test_the_newest_version_tree_wins(self, harness: _Harness) -> None:
        harness.plant_native("3.9.1")
        newest = harness.plant_native("3.13.2")

        assert harness.resolve().stdout == str(newest)


class TestDiskCache:
    def test_the_resolution_is_recorded_on_disk(self, harness: _Harness) -> None:
        native = harness.plant_native()

        harness.resolve()

        assert harness.cache_file.read_text(encoding="utf-8").splitlines()[-1] == str(native)

    def test_a_cache_hit_spawns_no_candidate(self, harness: _Harness) -> None:
        harness.plant_native()
        harness.resolve()
        harness.clear_probes()

        result = harness.resolve()

        assert result.returncode == 0
        assert harness.probes == []

    def test_a_new_candidate_invalidates_the_cache(self, harness: _Harness) -> None:
        harness.plant_installer_target()
        first = harness.resolve().stdout
        newer = harness.plant_native()

        assert harness.resolve().stdout == str(newer) != first

    def test_a_cached_path_that_no_longer_runs_is_rejected(self, harness: _Harness) -> None:
        native = harness.plant_native()
        harness.resolve()
        native.unlink()
        fallback = harness.plant_installer_target()

        assert harness.resolve().stdout == str(fallback)

    def test_an_unwritable_cache_dir_never_fails_the_resolution(self, harness: _Harness) -> None:
        native = harness.plant_native()

        result = harness.resolve(extra_env={"XDG_CACHE_HOME": "/proc/nonexistent-cache-root"})

        assert result.returncode == 0
        assert result.stdout == str(native)


class TestOperatorOverride:
    def test_t3_uv_wins_over_a_cached_entry(self, harness: _Harness) -> None:
        harness.plant_native()
        harness.resolve()
        override = _executable(harness.home / "elsewhere" / "uv", _NATIVE_BODY)

        result = harness.resolve(extra_env={"T3_UV": str(override)})

        assert result.stdout == str(override)

    def test_t3_uv_is_not_written_to_the_cache(self, harness: _Harness) -> None:
        override = _executable(harness.home / "elsewhere" / "uv", _NATIVE_BODY)

        harness.resolve(extra_env={"T3_UV": str(override)})

        assert not harness.cache_file.exists()


class TestForeignProjectEnvGuardSurvives:
    """The cache answers "which BINARY" only — the ``.venv`` guard stays per-invocation.

    Memoising the environment decision as well would reintroduce exactly the
    destruction ``uv_project_run_prefix`` exists to prevent: the boundary it
    inspects (a bind-mounted tree whose ``.venv`` belongs to the other side) can
    change while every candidate stays byte-identical.
    """

    def _prefix(self, harness: _Harness, project: Path) -> str:
        script = (
            f'set -u; . "{_LIB}"; uv_project_run_prefix /bin/true "{project}"; printf "%s\\n" "${{UV_PROJECT_RUN[@]}}"'
        )
        return subprocess.run(
            [_BASH, "-c", script],
            capture_output=True,
            text=True,
            env=harness.env,
            check=False,
        ).stdout

    def test_a_foreign_venv_redirects_to_the_hook_environment(self, harness: _Harness, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".venv").mkdir(parents=True)
        (project / ".venv" / "pyvenv.cfg").write_text("home = /nonexistent/other/side/bin\n", encoding="utf-8")

        assert "UV_PROJECT_ENVIRONMENT=.venv-hook" in self._prefix(harness, project)

    def test_a_local_venv_keeps_uv_managing_the_project(self, harness: _Harness, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".venv").mkdir(parents=True)
        home = Path(sys.executable).parent
        (project / ".venv" / "pyvenv.cfg").write_text(f"home = {home}\n", encoding="utf-8")

        assert "UV_PROJECT_ENVIRONMENT" not in self._prefix(harness, project)
