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


# Provably-non-invocation text — the legitimate over-block carve-outs (#2387).
_STILL_ALLOWED = [
    pytest.param("cat >> note.md <<EOF\nto land the PR run gh pr merge 5\nEOF", id="heredoc-documents"),
    pytest.param('echo "run gh pr merge 5 to land the PR"', id="echo-quoted"),
    pytest.param("ls  # gh pr merge 5", id="comment"),
    pytest.param("gh pr view 3", id="unrelated-forge-read"),
]

# Every plausible invocation form — each MUST stay DENIED (the cold-review
# evasion matrix). The id names the handling whose removal would leak the form.
_STILL_BLOCKED = [
    pytest.param("gh pr merge 5 --squash", id="bare-gh"),
    pytest.param("glab mr merge !9", id="bare-glab"),
    pytest.param("GH_TOKEN=x gh pr merge 5", id="env-assignment-prefix"),
    pytest.param("command gh pr merge 5", id="wrapper-command"),
    pytest.param("time gh pr merge 5", id="wrapper-time"),
    pytest.param("nohup gh pr merge 5", id="wrapper-nohup"),
    pytest.param("exec gh pr merge 5", id="wrapper-exec"),
    pytest.param("xargs gh pr merge", id="wrapper-xargs"),
    pytest.param("env gh pr merge 5", id="wrapper-env"),
    pytest.param("/usr/bin/gh pr merge 5", id="path-qualified-basename"),
    pytest.param("echo $(gh pr merge 5)", id="command-substitution-dollar"),
    pytest.param("echo `gh pr merge 5`", id="command-substitution-backtick"),
    pytest.param("( gh pr merge 5 )", id="subshell-group"),
    pytest.param("{ gh pr merge 5; }", id="brace-group"),
    pytest.param("if true; then gh pr merge 5; fi", id="compound-if-then"),
]


@pytest.mark.parametrize("command", _STILL_ALLOWED)
def test_documentation_or_mention_is_allowed(command: str, tmp_path: Path, home_managed: Path) -> None:
    repo = _managed_repo(tmp_path / "wt")
    result = _drive(command, repo, home_managed)
    assert result.returncode == 0, (
        "provably-non-invocation text must NOT be denied; "
        f"got exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("command", _STILL_BLOCKED)
def test_plausible_invocation_is_blocked(command: str, tmp_path: Path, home_managed: Path) -> None:
    repo = _managed_repo(tmp_path / "wt")
    result = _drive(command, repo, home_managed)
    assert result.returncode == 2, (
        "a plausible raw-merge invocation on a teatree-managed repo must be DENIED; "
        f"got exit {result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    decision = json.loads(result.stdout)
    assert decision["permissionDecision"] == "deny"
    assert "ticket merge" in decision["permissionDecisionReason"]
