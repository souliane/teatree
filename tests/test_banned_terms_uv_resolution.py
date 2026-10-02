"""``check-banned-terms.sh`` must not be wedged by a version-manager ``uv`` shim.

The hook runs with its CWD inside an ARBITRARY repo. When ``uv`` is installed
under a version manager, the PATH entry is a SHIM that selects its interpreter
from THAT repo's ``.python-version``; if the pinned version is not installed the
shim exits 127 before uv is ever executed. The hook used to ``exec`` whatever
``command -v uv`` returned, so the 127 propagated, the in-process gate read it
as "the scanner could not run", and EVERY commit in such a repo was blocked —
while uv itself was installed and healthy, and the deny message told the
operator to install uv.

These tests drive the real script against a planted broken shim and assert the
gate keeps working (and keeps CATCHING a planted term — a quiet gate is not a
correct one), plus that a genuinely unresolvable uv still fails CLOSED with a
message naming interpreter RESOLUTION rather than a matcher import.

Every probe root the resolver searches is HOME / ``PYENV_ROOT`` /
``ASDF_DATA_DIR``-relative, so redirecting those makes these runs hermetic
against whatever uv the developer machine really has.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests._uv_stub import executable as _executable
from tests._uv_stub import working_uv as _working_uv

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "hooks" / "check-banned-terms.sh"

_CLEAN_EXIT = 0
_BANNED_TERM_EXIT = 1
_SCANNER_UNAVAILABLE_EXIT = 2

_BANNED_TERM = "acmecorp"

# What the pyenv shim actually prints and exits with when the CWD repo pins an
# interpreter pyenv does not have installed.
_SHIM_BODY = """#!/usr/bin/env bash
echo "pyenv: version \\`3.14' is not installed (set by $PWD/.python-version)" >&2
echo "pyenv: uv: command not found" >&2
exit 127
"""


def _broken_uv_shim(bindir: Path) -> Path:
    return _executable(bindir / "uv", _SHIM_BODY)


def _recording_uv(path: Path, log: Path) -> Path:
    """A ``uv`` that answers ``--version`` and writes its own argv to *log*."""
    body = f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}}" = "--version" ]; then echo "uv 0.0.0-test"; exit 0; fi
printf '%s\\n' "$@" > {log}
exit 0
"""
    return _executable(path, body)


def _incapable_python(bindir: Path) -> Path:
    """A ``python3`` that starts but cannot import the matcher (the #1954 shape)."""
    body = '#!/usr/bin/env bash\necho "ImportError: PEP 604" >&2\nexit 1\n'
    return _executable(bindir / "python3", body)


def _seed_db(tmp_path: Path) -> Path:
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
        (json.dumps([_BANNED_TERM]),),
    )
    conn.commit()
    conn.close()
    return db


def _sample(tmp_path: Path, name: str, text: str) -> Path:
    sample = tmp_path / name
    sample.write_text(text + "\n", encoding="utf-8")
    return sample


def _hermetic_env(tmp_path: Path, bindir: Path, db: Path, **extra: str) -> dict[str, str]:
    """PATH holds only the planted binaries; every probe root points inside *tmp_path*."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "HOME": str(home),
        "PYENV_ROOT": str(tmp_path / "pyenv"),
        "ASDF_DATA_DIR": str(tmp_path / "asdf"),
        "T3_CONFIG_DB": str(db),
    }
    env.update(extra)
    return env


def _run(sample: Path, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_SCRIPT), str(sample)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=str(cwd),
    )


@pytest.mark.integration
class TestBrokenUvShimDoesNotWedgeTheGate:
    def test_shim_falls_through_to_a_real_uv_and_passes_clean_text(self, tmp_path: Path) -> None:
        bindir = tmp_path / "bin"
        _broken_uv_shim(bindir)
        _working_uv(tmp_path / "pyenv" / "versions" / "3.13.11" / "bin" / "uv")
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "clean.txt", "ship the docs refresh next week")

        result = _run(sample, _hermetic_env(tmp_path, bindir, db), cwd=tmp_path)

        assert result.returncode == _CLEAN_EXIT, (
            f"a broken uv shim must not wedge the gate, got {result.returncode}\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    def test_shim_falls_through_to_a_real_uv_and_still_catches_a_planted_term(self, tmp_path: Path) -> None:
        # Anti-vacuity: the fix must keep the gate CORRECT, not merely quiet.
        bindir = tmp_path / "bin"
        _broken_uv_shim(bindir)
        _working_uv(tmp_path / "pyenv" / "versions" / "3.13.11" / "bin" / "uv")
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "planted.txt", f"we ship to {_BANNED_TERM} next week")

        result = _run(sample, _hermetic_env(tmp_path, bindir, db), cwd=tmp_path)

        assert result.returncode == _BANNED_TERM_EXIT, (
            f"a planted term must still be caught (exit {_BANNED_TERM_EXIT}), got {result.returncode}\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "BANNED TERM in" in result.stdout

    def test_t3_uv_override_is_used_when_path_holds_only_a_shim(self, tmp_path: Path) -> None:
        bindir = tmp_path / "bin"
        _broken_uv_shim(bindir)
        override = _working_uv(tmp_path / "elsewhere" / "uv")
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "planted.txt", f"we ship to {_BANNED_TERM} next week")

        env = _hermetic_env(tmp_path, bindir, db, T3_UV=str(override))
        result = _run(sample, env, cwd=tmp_path)

        assert result.returncode == _BANNED_TERM_EXIT, (
            f"the T3_UV override must resolve, got {result.returncode}\nstderr={result.stderr!r}"
        )

    def test_shim_with_no_real_uv_falls_back_to_a_capable_python3(self, tmp_path: Path) -> None:
        # No uv anywhere the resolver looks, but the runner's python3 can import
        # the matcher — the documented fallback, which the 127 used to preempt.
        bindir = tmp_path / "bin"
        _broken_uv_shim(bindir)
        _executable(bindir / "python3", f'#!/usr/bin/env bash\nexec {sys.executable} "$@"\n')
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "planted.txt", f"we ship to {_BANNED_TERM} next week")

        result = _run(sample, _hermetic_env(tmp_path, bindir, db), cwd=tmp_path)

        assert result.returncode == _BANNED_TERM_EXIT, (
            f"the python3 fallback must run, got {result.returncode}\nstderr={result.stderr!r}"
        )


@pytest.mark.integration
class TestScanRunNeverResolvesDependencies:
    """The per-scan ``uv run`` must not re-resolve the lockfile.

    Resolution is the unbounded part of a scan, it runs on EVERY invocation, and it
    is charged against the gate's own subprocess budget — so a slow venue turns it
    into a timeout, and the gate blocks clean content. ``--no-sync`` is deliberately
    NOT the flag: the hook redirects to its own ``UV_PROJECT_ENVIRONMENT`` that
    nothing else provisions, so skipping the sync outright makes a fresh venue fail
    closed permanently instead of intermittently.
    """

    def _recorded_argv(self, tmp_path: Path) -> list[str]:
        log = tmp_path / "argv.txt"
        override = _recording_uv(tmp_path / "elsewhere" / "uv", log)
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "clean.txt", "nothing interesting here")
        env = _hermetic_env(tmp_path, tmp_path / "bin", db, T3_UV=str(override))
        _run(sample, env, cwd=tmp_path)
        return log.read_text(encoding="utf-8").split()

    def test_the_run_is_frozen(self, tmp_path: Path) -> None:
        assert "--frozen" in self._recorded_argv(tmp_path)

    def test_the_environment_is_still_synced(self, tmp_path: Path) -> None:
        assert "--no-sync" not in self._recorded_argv(tmp_path)


@pytest.mark.integration
class TestUnresolvableInterpreterFailsClosedAndNamesResolution:
    def test_exits_fail_closed_never_the_ambiguous_shim_code(self, tmp_path: Path) -> None:
        bindir = tmp_path / "bin"
        _broken_uv_shim(bindir)
        _incapable_python(bindir)
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "clean.txt", "nothing interesting here")

        result = _run(sample, _hermetic_env(tmp_path, bindir, db), cwd=tmp_path)

        assert result.returncode == _SCANNER_UNAVAILABLE_EXIT, (
            f"an unresolvable interpreter must fail CLOSED with the dedicated code, "
            f"got {result.returncode}\nstderr={result.stderr!r}"
        )

    def test_message_names_resolution_not_a_matcher_import(self, tmp_path: Path) -> None:
        # The old message ("install uv, or a Python >= 3.13, so the scanner can
        # import the matcher") sent the operator to reinstall a healthy uv.
        bindir = tmp_path / "bin"
        _broken_uv_shim(bindir)
        _incapable_python(bindir)
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "clean.txt", "nothing interesting here")

        result = _run(sample, _hermetic_env(tmp_path, bindir, db), cwd=tmp_path)
        loud = (result.stderr + result.stdout).lower()

        assert "resolution" in loud, f"the reason must name RESOLUTION, got {result.stderr!r}"
        assert "shim" in loud, f"the reason must name the shim cause, got {result.stderr!r}"
        assert "t3_uv" in loud, f"the reason must name the override escape, got {result.stderr!r}"
