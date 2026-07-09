# test-path: cross-cutting — drives hook_router.py (hooks/) as a subprocess; no src/teatree/ mirror.
"""SUBPROCESS-level liveness: the unknown-repo push gate fires in the real hook process.

The PreToolUse hook runs as a fresh subprocess that never calls
``django.setup()`` on its own. The gate resolves overlays via
``get_all_overlays()``, which trips the Django app registry — so without an
explicit bootstrap in ``_classify_push_for_cwd`` the registry error is swallowed
and EVERY push fails open (exit 0, no deny). The gate was production-dead while
the in-process pytest-django tests passed, because pytest-django bootstraps the
registry in-process.

This test drives the real ``hook_router.main()`` in a subprocess (the exact
``--event PreToolUse`` harness shape) so the registry starts un-bootstrapped,
exactly as the live hook runs. With the gate enabled and an overlay opted in,
a push to an UNKNOWN repo must DENY (exit 2). The in-process test cannot catch
this class — the subprocess is the real guard.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"
_REPO_ROOT = HOOK_ROUTER.parent.parent.parent

# Opt the always-registered t3-teatree overlay into the SCOPE gate, and disable
# the orchestrator-bash heavy-command gate so it does not deny the `git push`
# first and mask the gate under test.
_OPTED_IN_ROWS: dict[str, object] = {
    "unknown_repo_push_gate_enabled": True,
    "orchestrator_bash_gate_enabled": False,
    "overlays": {"t3-teatree": {"require_owned_repo_approval": True, "owned_repos": {"github.com": ["souliane"]}}},
}


def _seed_config_db(path: Path, rows: dict[str, object], scope: str = "") -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS teatree_config_setting "
        "(id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
    )
    for key, value in rows.items():
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES (?, ?, ?)",
            (scope, key, json.dumps(value)),
        )
    conn.commit()
    conn.close()


_DRIVER = """
import io, sys, json
import hooks.scripts.hook_router as r
sys.argv = ["hook_router.py", "--event", "PreToolUse"]
sys.stdin = io.StringIO(json.dumps({payload}))
r.main()
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def _repo_with_remote(path: Path, remote_url: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", remote_url)
    return path


@pytest.fixture
def home_with_opt_in() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_config_db(home / "config.sqlite3", _OPTED_IN_ROWS)
        yield home


def _drive_push(repo: Path, home: Path) -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin HEAD"},
        "cwd": str(repo),
        "session_id": "subproc-scope-live",
    }
    code = _DRIVER.format(payload=json.dumps(payload))
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(_REPO_ROOT),
            "T3_CONFIG_DB": str(home / "config.sqlite3"),
        },
    )


def test_unknown_repo_push_denies_in_the_real_subprocess(tmp_path: Path, home_with_opt_in: Path) -> None:
    repo = _repo_with_remote(tmp_path / "unk", "git@github.com:randomuser/randomrepo.git")
    result = _drive_push(repo, home_with_opt_in)
    assert result.returncode == 2, (
        "the unknown-repo SCOPE gate must DENY in the un-bootstrapped hook subprocess; "
        f"got exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    decision = json.loads(result.stdout)
    assert decision["permissionDecision"] == "deny"
    assert "owned_repos" in decision["permissionDecisionReason"]


def test_owned_repo_push_allows_in_the_real_subprocess(tmp_path: Path, home_with_opt_in: Path) -> None:
    repo = _repo_with_remote(tmp_path / "own", "git@github.com:souliane/teatree.git")
    result = _drive_push(repo, home_with_opt_in)
    assert result.returncode == 0, (
        f"a push to an OWNED repo must not deny; got exit {result.returncode}, stderr={result.stderr!r}"
    )
