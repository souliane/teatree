# test-path: cross-cutting — drives hooks/scripts/hook_router.py as a subprocess; no src/teatree/ mirror.
"""SUBPROCESS-level liveness for the action-aware out-of-band-merge gate (#2387).

The gate used to match ``gh pr merge`` / ``glab mr merge`` as a SUBSTRING of the
Bash command text, so a ``cat >> note.md <<EOF … gh pr merge … EOF`` heredoc that
merely DOCUMENTED the merge command was wrongly DENIED. The fix makes the matcher
action-aware: it fires only on an actual invocation at a command position.

These tests drive the real ``hook_router.main()`` in a subprocess (the exact
``--event PreToolUse`` harness the live hook runs) against a real ``git init``
repo whose remote is teatree-managed, so the managed-repo classifier resolves
offline from a tmp ``~/.teatree.toml``. A deny is ``exit 2``; an allow is
``exit 0``.
"""

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

HOOK_ROUTER = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "hook_router.py"
_REPO_ROOT = HOOK_ROUTER.parent.parent.parent

# Mark teatree core's own slug managed AND disable the heavy orchestrator-bash
# command gate so it cannot deny `cat`/`echo` first and mask the gate under test.
_MANAGED_TOML = """\
[teatree]
orchestrator_bash_gate_enabled = false

[overlays.example]
workspace_repos = ["souliane/teatree"]
"""

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


def _managed_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "remote", "add", "origin", "git@github.com:souliane/teatree.git")
    return path


@pytest.fixture
def home_managed() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / ".teatree.toml").write_text(_MANAGED_TOML, encoding="utf-8")
        yield home


def _drive(command: str, repo: Path, home: Path) -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(repo),
        "session_id": "subproc-oob-merge",
    }
    code = _DRIVER.format(payload=json.dumps(payload))
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONPATH": str(_REPO_ROOT)},
    )


def test_heredoc_documenting_merge_is_allowed(tmp_path: Path, home_managed: Path) -> None:
    repo = _managed_repo(tmp_path / "wt")
    command = "cat >> note.md <<EOF\nto land the PR run gh pr merge 5\nEOF"
    result = _drive(command, repo, home_managed)
    assert result.returncode == 0, (
        "a heredoc that only DOCUMENTS the merge command must NOT be denied; "
        f"got exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def test_echo_of_merge_phrase_is_allowed(tmp_path: Path, home_managed: Path) -> None:
    repo = _managed_repo(tmp_path / "wt")
    result = _drive('echo "run gh pr merge 5 to land the PR"', repo, home_managed)
    assert result.returncode == 0, (
        f"an echo of the merge phrase must NOT be denied; got exit {result.returncode}, stderr={result.stderr!r}"
    )


def test_merge_phrase_in_comment_is_allowed(tmp_path: Path, home_managed: Path) -> None:
    repo = _managed_repo(tmp_path / "wt")
    result = _drive("ls  # gh pr merge 5", repo, home_managed)
    assert result.returncode == 0, (
        f"the merge phrase inside a # comment must NOT be denied; "
        f"got exit {result.returncode}, stderr={result.stderr!r}"
    )


def test_real_raw_merge_is_still_blocked(tmp_path: Path, home_managed: Path) -> None:
    repo = _managed_repo(tmp_path / "wt")
    result = _drive("gh pr merge 5 --squash", repo, home_managed)
    assert result.returncode == 2, (
        "an actual raw `gh pr merge` on a teatree-managed repo must STILL be DENIED; "
        f"got exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    decision = json.loads(result.stdout)
    assert decision["permissionDecision"] == "deny"
    assert "ticket merge" in decision["permissionDecisionReason"]
