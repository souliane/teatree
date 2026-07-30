"""The ``check-banned-terms.sh`` fallback must FAIL CLOSED, never ALLOW (#1954).

The shell hook prefers ``uv run`` but falls back to a bare ``python3 -m
teatree.hooks.banned_terms_cli`` when ``uv`` is absent. The repo requires
Python >= 3.13; under an old system ``python3`` the matcher import crashes
(PEP-604 unions), the process exits 1 — which COLLIDES with the contract's
"banned term found" code — and the in-process caller, parsing an empty
stdout report, turned that crash into ALLOW. A security gate that fails open
on a crash is the bug class.

The fix: the fallback probes the interpreter before running the scanner and,
when it cannot run it, exits with the dedicated FAIL-CLOSED code (2) and a
loud error — distinct from 0 (clean) and 1 (banned term found). These tests
drive the script under a deliberately-incapable interpreter and assert the
fail-closed exit, and confirm the happy path (a capable interpreter) still
returns 0/1. The term list is DB-home, injected via ``T3_CONFIG_DB``.
"""

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "hooks" / "check-banned-terms.sh"

# The dedicated FAIL-CLOSED exit code the fallback uses when it cannot run the
# scanner. Distinct from 0 (clean) and 1 (banned term found).
_SCANNER_UNAVAILABLE_EXIT = 2


def _seed_db(tmp_path: Path, terms: tuple[str, ...] = ("acmecorp",)) -> Path:
    db = tmp_path / "config.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
        "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_terms', ?)",
        (json.dumps(list(terms)),),
    )
    conn.commit()
    conn.close()
    return db


def _sample(tmp_path: Path, text: str) -> Path:
    sample = tmp_path / "sample.txt"
    sample.write_text(text + "\n", encoding="utf-8")
    return sample


def _fake_python(tmp_path: Path, *, body: str) -> Path:
    """Write a shimmed ``python3`` on a clean PATH so the fallback resolves to it."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "python3"
    shim.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    # The fallback also needs real tools (env, bash) — keep them on PATH.
    return bindir


def _run_without_uv(script_args: list[str], *, fake_bin: Path, db: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the script with ``uv`` removed from PATH so the python3 fallback fires."""
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/tmp"),
        "T3_CONFIG_DB": str(db),
    }
    return _invoke_script(script_args, env=env)


def _invoke_script(script_args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke the executable hook script directly by its absolute path."""
    return subprocess.run(
        [str(_SCRIPT), *script_args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _real_python_env_without_uv(db: Path) -> dict[str, str]:
    """Inherit the runner env but drop ``uv`` from PATH so the python3 fallback fires."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["PATH"] = ":".join(p for p in env.get("PATH", "").split(":") if "uv" not in p.lower())
    env["T3_CONFIG_DB"] = str(db)
    return env


@pytest.mark.integration
class TestFallbackFailsClosedOnIncapableInterpreter:
    def test_import_crash_exits_fail_closed_not_clean(self, tmp_path: Path) -> None:
        # The interpreter starts but crashes importing the module (exit 1,
        # traceback on stderr, nothing on stdout) — the exact old-python3 shape.
        fake_bin = _fake_python(
            tmp_path,
            body='echo "Traceback (most recent call last):" >&2\necho "ImportError: PEP 604" >&2\nexit 1\n',
        )
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "we ship to acmecorp")
        result = _run_without_uv([str(sample)], fake_bin=fake_bin, db=db)
        assert result.returncode == _SCANNER_UNAVAILABLE_EXIT, (
            f"crash must fail CLOSED (exit {_SCANNER_UNAVAILABLE_EXIT}), got {result.returncode}\n"
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert result.returncode != 0, "a scanner crash must never resolve to ALLOW (exit 0)"

    def test_old_version_interpreter_exits_fail_closed(self, tmp_path: Path) -> None:
        # The interpreter reports a version below the required floor. The
        # fallback must refuse to run the scanner and fail closed.
        fake_bin = _fake_python(
            tmp_path,
            body=(
                'if [[ "$*" == *"--version"* || "$1" == "-V" ]]; then echo "Python 3.9.6"; exit 0; fi\n'
                'if [[ "$*" == *"sys.version_info"* ]]; then echo "3 9"; exit 0; fi\n'
                "exit 1\n"
            ),
        )
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "we ship to acmecorp")
        result = _run_without_uv([str(sample)], fake_bin=fake_bin, db=db)
        assert result.returncode == _SCANNER_UNAVAILABLE_EXIT, (
            f"old interpreter must fail CLOSED, got {result.returncode}\nstderr={result.stderr!r}"
        )

    def test_fail_closed_prints_a_loud_error(self, tmp_path: Path) -> None:
        fake_bin = _fake_python(tmp_path, body="exit 1\n")
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "clean text")
        result = _run_without_uv([str(sample)], fake_bin=fake_bin, db=db)
        assert result.returncode == _SCANNER_UNAVAILABLE_EXIT
        loud = (result.stderr + result.stdout).lower()
        assert "scanner" in loud or "python" in loud or "interpreter" in loud, (
            f"a fail-closed must be diagnosable, got stderr={result.stderr!r}"
        )


@pytest.mark.integration
class TestFallbackHappyPathUnderCapableInterpreter:
    """A capable real interpreter still returns the 0/1 contract codes."""

    def test_clean_text_exits_zero(self, tmp_path: Path) -> None:
        # The real ``python3`` on the test runner IS >= 3.13, so the fallback
        # runs the scanner for real: clean text ⇒ exit 0.
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "ship the docs refresh next week")
        result = _invoke_script([str(sample)], env=_real_python_env_without_uv(db))
        assert result.returncode == 0, f"clean text must pass, got {result.returncode}\nstderr={result.stderr!r}"

    def test_banned_term_exits_one(self, tmp_path: Path) -> None:
        db = _seed_db(tmp_path)
        sample = _sample(tmp_path, "we ship to acmecorp next week")
        result = _invoke_script([str(sample)], env=_real_python_env_without_uv(db))
        assert result.returncode == 1, f"banned term must flag (exit 1), got {result.returncode}\n{result.stderr!r}"
